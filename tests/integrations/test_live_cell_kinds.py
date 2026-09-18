"""The read-shape declaration is checked, not trusted (#73).

`t03_live_cells._KINDS` decides what the doubles materialize; a field it does not
declare reaches the read side as a raw write payload. These tests keep the declaration
tied to the read side (``t03_read_sites``) and to the live field types observed
read-only (``fixtures/t73_live_field_types.json``), and they fail loudly when a select
field is dropped or declared as another shape.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t031_fake_dws import load_state, save_state
from t03_kinds_guard import (KindsGuardError, check, live_type_fixture,
                             observed_live_types, unobserved_notes, verify)
from t03_layout import entry_fields_from
from t03_live_cells import (DATETIME, KINDS_DECLARATION, KindsError, NUMBER, PERSON,
                            SINGLE_SELECT, TEXT, declared_kinds, validate_state_kinds)
from t03_read_sites import (ReadSiteError, map_attributes, map_attributes_by_class,
                            read_sites, reader_sources_outside, scan)

from contracts.model import Code, ContractError, Resource
from integrations.dingtalk.adapter import DingTalkAdapter
from integrations.dingtalk.codec import decode_loan, encode_loan
from integrations.dingtalk.dws_transport import DwsTransport
from integrations.dingtalk.layout import (SYNTHETIC_APPLY_FIELDS, SYNTHETIC_ENTRY_FIELDS,
                                          SYNTHETIC_FIELDS, SYNTHETIC_RETURN_FORM_FIELDS)

ROOT = Path(__file__).resolve().parents[2]
FAKE_DWS = Path(__file__).resolve().parent / 't031_fake_dws.py'
LOAN = Resource('record', 'synthetic-org', 'baseLoan/tblLoan', 'recLoan')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fixtures = _load('t73_kinds_fixtures', ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t73_kinds_synthetic', ROOT / 'tests' / 'contracts' / 'synthetic.py')

ALL_MAPS = (SYNTHETIC_FIELDS, SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_APPLY_FIELDS,
            SYNTHETIC_RETURN_FORM_FIELDS)


def loan():
    """The synthetic loan on a container id the transport will split."""
    return replace(_fixtures.loan(), ref=LOAN)


def declared(declaration=KINDS_DECLARATION):
    """``{attribute: shape}`` for a declaration, for mutation tests."""
    shapes = {}
    for shape, attributes in declaration:
        for attribute in attributes:
            shapes[attribute] = shape
    return shapes


def redeclared(declaration=KINDS_DECLARATION, **changes):
    """The declaration with ``attribute=shape`` overrides applied (``None`` drops)."""
    wanted = dict(declared(declaration))
    for attribute, shape in changes.items():
        if shape is None:
            wanted.pop(attribute, None)
        else:
            wanted[attribute] = shape
    grouped = {}
    for attribute, shape in wanted.items():
        grouped.setdefault(shape, []).append(attribute)
    return tuple((shape, tuple(sorted(attributes))) for shape, attributes in sorted(grouped.items()))


def mentions(problems, *fragments):
    return [problem for problem in problems if all(fragment in problem for fragment in fragments)]


class KindsDeclarationTests(unittest.TestCase):
    def test_declaration_agrees_with_the_read_side_and_the_live_types(self):
        self.assertEqual(check(), [])
        self.assertTrue(verify())

    def test_every_field_map_attribute_is_declared_exactly_once(self):
        self.assertEqual(set(declared()), map_attributes())

    def test_read_scan_pins_the_production_readers(self):
        sites = read_sites()
        self.assertEqual(sites.problems, [])
        self.assertEqual(sites.sites['state'], {SINGLE_SELECT})
        self.assertEqual(sites.sites['tracked'], {SINGLE_SELECT})
        self.assertEqual(sites.sites['decision'], {SINGLE_SELECT})
        self.assertEqual(sites.sites['borrower'], {PERSON})
        self.assertEqual(sites.sites['approver'], {PERSON})
        self.assertEqual(sites.sites['manager'], {PERSON})
        self.assertEqual(sites.sites['quantity'], {NUMBER})
        self.assertEqual(sites.sites['available'], {NUMBER})
        self.assertEqual(sites.sites['due_at'], {DATETIME})
        # Read strictly once and only checked for presence in the form path.
        self.assertEqual(sites.sites['occurred_at'], {DATETIME, TEXT})
        self.assertEqual(sites.sites['return_container'], {TEXT})
        self.assertEqual(sites.sites['return_id'], {TEXT})
        # #77 的可选「归还物品」格：`_return_item_value` 按单选对象读（纯字符串也容忍）。
        self.assertEqual(sites.sites['item'], {SINGLE_SELECT})

    def test_no_production_reader_call_lives_outside_the_scanned_roots(self):
        self.assertEqual(reader_sources_outside(), [])

    def test_the_observation_fixture_covers_every_declared_field(self):
        """Every declared field is observed, or has a written「真机类型未观测」reason."""
        observed = observed_live_types()
        noted = {attribute for table in unobserved_notes(live_type_fixture()).values()
                  for attribute in table}
        self.assertEqual(sorted(set(declared()) - set(observed) - noted), [])
        self.assertNotIn('', observed)
        self.assertNotIn('', noted)

    def test_the_observation_fixture_lists_a_table_for_every_field_map(self):
        fixture = live_type_fixture()
        self.assertEqual(sorted(fixture['_tables_for_map']), sorted(map_attributes_by_class()))

    def test_declared_kinds_materialize_every_mapping_id(self):
        kinds = declared_kinds(*ALL_MAPS)
        for fields in ALL_MAPS:
            for attribute, shape in declared().items():
                field_id = getattr(fields, attribute, None)
                if not isinstance(field_id, str) or not field_id:
                    continue
                if shape in (SINGLE_SELECT, PERSON, NUMBER):
                    self.assertEqual(kinds[field_id], shape,
                                     f'{type(fields).__name__}.{attribute}')
                else:
                    self.assertNotIn(field_id, kinds,
                                     f'{type(fields).__name__}.{attribute}')

    def test_dropping_a_select_from_the_declaration_fails_loudly(self):
        problems = check(declaration=redeclared(state=None))
        self.assertTrue(mentions(problems, 'state', '没有声明读形态'), problems)
        self.assertTrue(mentions(problems, 'state', '没有声明'), problems)

    def test_declaring_a_select_as_number_fails_loudly(self):
        problems = check(declaration=redeclared(state=NUMBER))
        self.assertTrue(mentions(problems, 'state', f'声明为 {NUMBER}'), problems)
        self.assertTrue(mentions(problems, 'state', 'singleSelect'), problems)

    def test_dropping_a_number_read_field_fails_loudly(self):
        problems = check(declaration=redeclared(quantity=None))
        self.assertTrue(mentions(problems, 'quantity'), problems)

    def test_declaring_a_text_field_as_select_fails_loudly(self):
        problems = check(declaration=redeclared(return_id=SINGLE_SELECT))
        self.assertTrue(mentions(problems, 'return_id'), problems)

    def test_a_new_field_map_attribute_cannot_inherit_pass_through(self):
        """The #73 case: a new number field added to a map and forgotten in the map."""
        sites = dict(read_sites().sites, tare_weight={NUMBER})
        problems = check(map_attrs=map_attributes() | {'tare_weight'}, sites=sites)
        self.assertTrue(mentions(problems, 'tare_weight', '没有声明读形态'), problems)
        self.assertTrue(mentions(problems, 'tare_weight', '读侧用到'), problems)

    def test_a_declared_name_that_is_not_a_field_map_attribute_fails_loudly(self):
        problems = check(declaration=redeclared() + ((TEXT, ('cycles',)),))
        self.assertTrue(mentions(problems, 'cycles'), problems)

    def test_a_duplicate_declaration_fails_loudly(self):
        problems = check(declaration=redeclared() + ((TEXT, ('state',)),))
        self.assertTrue(mentions(problems, 'state', '重复声明'), problems)

    def test_a_declared_field_without_an_observation_fails_loudly(self):
        observed = dict(observed_live_types())
        observed.pop('revision')
        problems = check(observed=observed)
        self.assertTrue(mentions(problems, 'revision', '没有真机字段类型观测'), problems)

    def test_an_unobserved_gap_without_a_note_fails_loudly(self):
        fixture = live_type_fixture()
        fixture['unobserved'] = {}
        problems = check(fixture=fixture)
        self.assertTrue(
            mentions(problems, 'physical_ids', '既没有观测，也没有'), problems)

    def test_the_optional_return_item_rests_on_its_written_gap_note(self):
        """#77's「归还物品」cell (#83/#89 sync): the stage-entry table still has no such field.

        #89 observed the application table's「工具」question read-only — same attribute
        name ``item``, live type ``singleSelect`` — so ``item`` is no longer an attribute
        with no observation anywhere. On **stage_entry** the question is still added by a
        human on the return form view and the table does not carry it yet, so that
        declaration continues to rest on the fixture's「真机类型未观测」note: not on a
        guessed live type, and not on a pass-through.
        """
        self.assertEqual(declared()['item'], SINGLE_SELECT)
        self.assertEqual(observed_live_types()['item'], frozenset({'singleSelect'}))
        fixture = live_type_fixture()
        self.assertIn('item', fixture['unobserved']['stage_entry'])
        self.assertNotIn('item', fixture['tables']['stage_entry'])
        self.assertEqual(
            declared_kinds(SYNTHETIC_RETURN_FORM_FIELDS)['fldSYN-return-item'],
            SINGLE_SELECT)

    def test_dropping_the_gap_note_for_an_unobserved_field_fails_loudly(self):
        fixture = json.loads(json.dumps(live_type_fixture()))
        fixture['unobserved']['stage_entry'].pop('item')
        problems = check(fixture=fixture)
        # #89 起 item 在申请表上有观测，所以这里亮的是「逐表」那条：阶段入口表上既没有
        # 观测也没有注记。全局那条由 test_a_declared_field_without_an_observation 守着。
        self.assertTrue(mentions(problems, 'item', '既没有观测，也没有'), problems)
        self.assertTrue(mentions(problems, 'ReturnFormFieldMap', 'stage_entry'), problems)

    def test_a_blank_gap_note_does_not_excuse_a_missing_observation(self):
        """A「未观测」note without a reason is the silent pass-through #73 removes."""
        fixture = json.loads(json.dumps(live_type_fixture()))
        fixture['unobserved']['stage_entry']['item'] = '   '
        problems = check(fixture=fixture)
        self.assertTrue(mentions(problems, 'item', '既没有观测，也没有'), problems)
        self.assertTrue(mentions(problems, 'ReturnFormFieldMap', 'stage_entry'), problems)

    def test_a_gap_note_never_excuses_a_shape_the_read_side_does_not_speak(self):
        """The note buys the missing observation only; the read side still rules."""
        problems = check(declaration=redeclared(item=TEXT))
        self.assertTrue(mentions(problems, 'item', '读侧却按 singleSelect 读'), problems)

    def test_a_live_type_contradiction_fails_loudly(self):
        """#73 item 3: had the platform answered `singleSelect`, this must go red."""
        observed = dict(observed_live_types())
        observed['return_container'] = frozenset({'singleSelect'})
        problems = check(observed=observed)
        self.assertTrue(mentions(problems, 'return_container', 'singleSelect'), problems)

    def test_an_unresolvable_read_site_fails_loudly(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'sample_module.py'
            source.write_text(
                'def pick(cells, fields):\n'
                "    field_id = fields.state if cells else 'SYNTHETIC-field'\n"
                '    return read_text(cells, field_id)\n',
                encoding='utf-8',
            )
            result = scan(roots=[Path(temp)])
            self.assertTrue(result.problems, result.problems)
            self.assertTrue(mentions(result.problems, '读侧字段无法静态解析'), result.problems)
            with self.assertRaises(ReadSiteError):
                read_sites(roots=[Path(temp)])

    def test_a_read_site_with_no_attribute_passing_caller_fails_loudly(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'sample_module.py'
            source.write_text(
                'def inner(cells, field_id):\n'
                '    return read_text(cells, field_id)\n'
                '\n'
                'def outer(cells, fields):\n'
                '    return inner(cells, fields.state)\n',
                encoding='utf-8',
            )
            result = scan(roots=[Path(temp)])
            self.assertEqual(result.problems, [])
            self.assertEqual(result.sites['state'], {TEXT})


class FakeDwsKindsDeclarationTests(unittest.TestCase):
    """The file double refuses to serve reads without a declared schema (#73 item 2)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.state_path = self.work / 'fake-state.json'
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = entry_fields_from(self.fields)
        state = load_state(self.state_path)
        state['records'] = {self.slot(): encode_loan(self.loan(), self.fields)}
        save_state(self.state_path, state)

    def slot(self):
        return f'{LOAN.container_id}/{LOAN.resource_id}'

    def base_and_table(self):
        base_id, table_id = LOAN.container_id.split('/')
        return base_id, table_id

    def loan(self):
        return replace(_fixtures.loan(), ref=LOAN)

    def query(self, state=None):
        if state is not None:
            save_state(self.state_path, state)
        base_id, table_id = self.base_and_table()
        return subprocess.run(
            [sys.executable, str(FAKE_DWS), 'aitable', 'record', 'query',
             '--base-id', base_id, '--table-id', table_id,
             '--record-ids', LOAN.resource_id, '--format', 'json'],
            capture_output=True, text=True, env=self.env(),
        )

    def env(self):
        return dict(os.environ, FAKE_DWS_STATE=str(self.state_path))

    def test_a_state_without_declared_kinds_is_refused(self):
        result = self.query(load_state(self.state_path))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), '')
        self.assertIn('kinds', result.stderr)

    def test_an_unknown_declared_kind_is_refused(self):
        state = load_state(self.state_path)
        state['kinds'] = {self.fields.state: 'SingleSelect'}
        result = self.query(state)
        self.assertEqual(result.returncode, 2)
        self.assertIn('SingleSelect', result.stderr)

    def test_declared_kinds_still_materialize_select_and_person_cells(self):
        state = load_state(self.state_path)
        state['kinds'] = declared_kinds(self.fields, self.entry_fields)
        result = self.query(state)
        self.assertEqual(result.returncode, 0, result.stderr)
        cells = json.loads(result.stdout)['data']['records'][0]['cells']
        self.assertEqual(cells[self.fields.state]['name'], self.loan().state.value)
        self.assertNotEqual(cells[self.fields.state]['id'], cells[self.fields.state]['name'])
        self.assertEqual(cells[self.fields.borrower],
                         [{'corpId': self.loan().borrower.tenant_id,
                           'userId': self.loan().borrower.user_id}])

    def test_the_adapter_surfaces_the_refusal_as_a_contract_error(self):
        transport = DwsTransport(
            [sys.executable, str(FAKE_DWS)], self.fields,
            form_container='baseForm/tblForm', todo_container='todoSpace/executors',
            work_dir=self.work / 'runtime', entry_fields=self.entry_fields,
            extra_env={'FAKE_DWS_STATE': str(self.state_path)},
        )
        adapter = DingTalkAdapter(transport, _synthetic.SyntheticJournal(),
                                  _synthetic.SyntheticLease(), self.fields,
                                  self.entry_fields)
        with self.assertRaises(ContractError) as caught:
            adapter.read_loan(self.loan().ref)
        self.assertEqual(caught.exception.code, Code.UNKNOWN)

    def test_declared_kinds_without_any_field_map_are_refused(self):
        for call in (lambda: declared_kinds(), lambda: declared_kinds(None, None)):
            with self.assertRaises(KindsError):
                call()

    def test_declared_kinds_skip_maps_that_were_not_given(self):
        kinds = declared_kinds(None, SYNTHETIC_APPLY_FIELDS)
        self.assertEqual(kinds[SYNTHETIC_APPLY_FIELDS.quantity], NUMBER)
        self.assertEqual(kinds[SYNTHETIC_APPLY_FIELDS.borrower], PERSON)
        self.assertNotIn(SYNTHETIC_FIELDS.state, kinds)

    def test_a_state_with_declared_kinds_and_an_unknown_shape_is_refused(self):
        state = load_state(self.state_path)
        state['kinds'] = {'': 'singleSelect'}
        self.assertEqual(self.query(state).returncode, 2)

    def test_a_state_declaring_no_kinds_key_at_all_is_refused(self):
        with self.assertRaises(KindsError):
            validate_state_kinds({'records': {}})


class ReturnFieldLiveTypeTests(unittest.TestCase):
    """#73 item 3: the two return fields had never been observed on the platform.

    A read-only ``dws aitable field get`` now answers ``text`` for both, on the ledger
    and on the stage-entry table, so ``read_text`` / ``read_text_or_empty`` is the
    right reader. These tests pin the observation and the fail-closed answer if the
    platform ever returns a select object for them anyway.
    """

    def test_both_return_fields_are_observed_as_text_and_read_as_text(self):
        fixture = live_type_fixture()
        for table in ('loan_ledger', 'stage_entry'):
            self.assertEqual(fixture['tables'][table]['return_container'], 'text')
            self.assertEqual(fixture['tables'][table]['return_id'], 'text')
        sites = read_sites().sites
        self.assertEqual(sites['return_container'], {TEXT})
        self.assertEqual(sites['return_id'], {TEXT})
        self.assertEqual(declared()['return_container'], TEXT)
        self.assertEqual(declared()['return_id'], TEXT)

    def test_a_select_shaped_return_id_fails_closed(self):
        fields = SYNTHETIC_FIELDS
        cells = encode_loan(loan(), fields)
        cells[fields.return_id] = {'id': 'SYNTHETIC-rand-return', 'name': 'SYNTHETIC-return'}
        with self.assertRaises(ContractError) as caught:
            decode_loan(loan().ref, cells, fields)
        self.assertEqual(caught.exception.code, Code.EVIDENCE)

    def test_a_select_shaped_return_container_fails_closed(self):
        fields = SYNTHETIC_FIELDS
        cells = encode_loan(loan(), fields)
        cells[fields.return_container] = {'id': 'SYNTHETIC-rand-container',
                                          'name': 'SYNTHETIC-returns'}
        with self.assertRaises(ContractError) as caught:
            decode_loan(loan().ref, cells, fields)
        self.assertEqual(caught.exception.code, Code.EVIDENCE)


if __name__ == '__main__':
    unittest.main()
