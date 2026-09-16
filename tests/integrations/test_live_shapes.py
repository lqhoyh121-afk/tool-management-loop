"""Live DingTalk shapes the in-memory fake used to hide: random select ids, omitted empties."""
import importlib.util
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_memory_transport import MemoryTransport

from contracts.flow import plan, verify
from contracts.model import Action, Code, ContractError, Outcome
from contracts.ports import LedgerScope, RuntimeBinding, StageRequest, stage_operation_id
from integrations.dingtalk.adapter import DingTalkAdapter
from integrations.dingtalk.codec import decode_loan, encode_inventory, encode_loan
from integrations.dingtalk.layout import SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_FIELDS


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t27_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t27_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
loan, stock, event = _fixtures.loan, _fixtures.stock, _fixtures.event
MANAGER, BORROWER, ITEM, LOAN, NOW = (
    _fixtures.MANAGER, _fixtures.BORROWER, _fixtures.ITEM, _fixtures.LOAN, _fixtures.NOW)
SyntheticJournal, SyntheticLease = _synthetic.SyntheticJournal, _synthetic.SyntheticLease


def _live_select(name):
    return {'id': f'SYNTHETIC-rand-{name}', 'name': name}


class LiveShapeTests(unittest.TestCase):
    def setUp(self):
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = SYNTHETIC_ENTRY_FIELDS
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = MemoryTransport(self.fields, self.entry_fields)
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields)
        self.transport.seed_record(loan().ref, encode_loan(loan(), self.fields))
        self.transport.seed_record(stock().ref, encode_inventory(stock(), self.fields))

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_decode_loan_uses_select_name_not_random_id(self):
        current = loan()
        cells = encode_loan(current, self.fields)
        cells[self.fields.state] = _live_select(current.state.value)
        cells[self.fields.tracked] = _live_select('false')
        self.assertNotEqual(cells[self.fields.state]['id'], cells[self.fields.state]['name'])
        self.assertEqual(decode_loan(current.ref, cells, self.fields), current)

    def test_decode_loan_tolerates_omitted_empty_return_fields(self):
        current = loan()
        cells = encode_loan(current, self.fields)
        cells[self.fields.state] = _live_select(current.state.value)
        cells[self.fields.tracked] = _live_select('false')
        cells.pop(self.fields.return_id, None)
        cells.pop(self.fields.return_container, None)
        self.assertEqual(decode_loan(current.ref, cells, self.fields).return_ref, None)

    def test_agree_decision_uses_select_name(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        key = ('synthetic-org', 'synthetic-forms', receipt.source.resource_id)
        self.transport.records[key][self.entry_fields.decision] = _live_select('agree')
        self.transport.records[key][self.entry_fields.occurred_at] = NOW.isoformat()
        agreed = self.adapter.read_event(loan(), receipt.source)
        self.assertEqual(agreed.action, Action.APPROVE)
        self.assertEqual(agreed.actor, MANAGER)

    def test_apply_decision_is_borrower_action_with_quantity(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        key = ('synthetic-org', 'synthetic-forms', receipt.source.resource_id)
        cells = self.transport.records[key]
        cells[self.entry_fields.decision] = _live_select('apply')
        cells[self.entry_fields.occurred_at] = NOW.isoformat()
        applied = self.adapter.read_event(loan(), receipt.source)
        self.assertEqual(applied.action, Action.APPLY)
        self.assertEqual(applied.actor, BORROWER)
        self.assertEqual(applied.quantity, loan().quantity)
        self.assertEqual(applied.physical_ids, loan().physical_ids)

    def test_create_stage_writes_entry_identities_and_quantity(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        key = ('synthetic-org', 'synthetic-forms', receipt.source.resource_id)
        cells = self.transport.records[key]
        self.assertIn(self.entry_fields.approver, cells)
        self.assertIn(self.entry_fields.manager, cells)
        self.assertIn(self.entry_fields.quantity, cells)
        self.assertIn(self.entry_fields.physical_ids, cells)
        self.assertNotIn(self.entry_fields.return_id, cells)
        self.assertNotIn(self.entry_fields.return_container, cells)

    def test_timeout_with_unchanged_records_can_retry(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.transport.drop_once.append('record.update')
        first = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(first.outcome, Outcome.UNKNOWN)
        self.assertEqual(len(self.transport.writes_of('record.update')), 1)
        self.assertEqual(self.adapter.query(intent).outcome, Outcome.NOT_SENT)
        second = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(verify(intent, second).outcome, Outcome.VERIFIED)
        self.assertEqual(len(self.transport.writes_of('record.update')), 3)

    def test_partial_write_still_blocks_retry(self):
        approved = plan(loan(), event(Action.APPROVE), stock())
        self.adapter.submit(approved, self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        inventory = self.adapter.read_inventory(ITEM)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        intent = plan(current, reserve, inventory)
        self.transport.drop_after_updates = self.transport.update_count + 1
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.UNKNOWN)
        writes = len(self.transport.writes_of('record.update'))
        self.blocked(Code.UNKNOWN, lambda: self.adapter.submit(intent, self.binding, self.lease))
        self.assertEqual(len(self.transport.writes_of('record.update')), writes)

    def test_encode_loan_writes_select_name_strings(self):
        current = loan()
        cells = encode_loan(current, self.fields)
        self.assertEqual(cells[self.fields.state], current.state.value)
        self.assertEqual(cells[self.fields.tracked], 'false')
        self.assertIsInstance(cells[self.fields.state], str)
        self.assertIsInstance(cells[self.fields.tracked], str)

    def test_synthetic_option_id_write_is_not_success(self):
        current = loan()
        cells = encode_loan(current, self.fields)
        cells[self.fields.state] = {
            'id': f'SYNTHETIC-opt-{current.state.value}',
            'name': current.state.value,
        }
        payload = self.transport.exchange('record.update', {
            'tenant_id': current.ref.tenant_id,
            'container_id': current.ref.container_id,
            'resource_id': current.ref.resource_id,
            'cells': cells,
        })
        self.assertEqual(payload['status'], 'error')
        self.assertEqual(payload['error']['code'], 'SELECT_OPTION_NOT_FOUND')
        self.assertNotEqual(payload['status'], 'success')

    def test_synthetic_option_id_submit_is_unknown(self):
        from unittest.mock import patch
        real_encode = encode_loan

        def poisoned(item, fields):
            cells = real_encode(item, fields)
            cells[fields.state] = {
                'id': f'SYNTHETIC-opt-{item.state.value}',
                'name': item.state.value,
            }
            return cells

        intent = plan(loan(), event(Action.APPROVE), stock())
        with patch('integrations.dingtalk.adapter.encode_loan', poisoned):
            receipt = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.UNKNOWN)
        self.assertNotEqual(receipt.outcome, Outcome.VERIFIED)


if __name__ == '__main__':
    unittest.main()
