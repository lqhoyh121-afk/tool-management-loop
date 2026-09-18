import importlib.util
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t04_binding_doc import binding_document

from bootstrap.binding import (binding_from_document, intake_since_from_document,
                               load_binding, save_binding)
from contracts.model import Code, ContractError
from contracts.ports import check_binding
from integrations.dingtalk.layout import (
    SYNTHETIC_APPLY_FIELDS, SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_FIELDS,
    SYNTHETIC_RETURN_FORM_FIELDS)


def _fixtures():
    path = Path(__file__).resolve().parents[2] / 'tests' / 'contracts' / 'fixtures.py'
    spec = importlib.util.spec_from_file_location('t04_contract_fixtures', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BindingTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_save_and_load_roundtrip(self):
        binding, entry = save_binding(self.root, binding_document())
        loaded, loaded_entry = load_binding(self.root)
        self.assertEqual(loaded, binding)
        self.assertEqual(loaded_entry, entry)
        self.assertEqual(entry.kind, 'form')

    def test_binding_from_document_returns_all_maps(self):
        binding, entry, fields, entry_fields, apply_fields, return_form_fields = (
            binding_from_document(binding_document()))
        self.assertEqual(fields, SYNTHETIC_FIELDS)
        self.assertEqual(entry_fields, SYNTHETIC_ENTRY_FIELDS)
        self.assertEqual(apply_fields, SYNTHETIC_APPLY_FIELDS)
        self.assertEqual(return_form_fields, SYNTHETIC_RETURN_FORM_FIELDS)
        self.assertEqual(entry.kind, 'form')
        self.assertIsNotNone(binding)

    def test_missing_flag_defaults_closed(self):
        data = binding_document()
        del data['explicitly_confirmed']
        self.blocked(Code.EVIDENCE, lambda: save_binding(self.root, data))
        self.assertFalse((self.root / 'binding.json').exists())

    def test_does_not_overwrite_existing_binding(self):
        save_binding(self.root, binding_document())
        self.blocked(Code.CONFIG, lambda: save_binding(self.root, binding_document()))

    def test_interrupted_tmp_is_not_binding(self):
        (self.root / 'binding.json.tmp').write_text('{', encoding='utf-8')
        self.blocked(Code.EVIDENCE, lambda: load_binding(self.root))
        self.blocked(Code.EVIDENCE, lambda: save_binding(self.root, binding_document()))

    def test_missing_fields_is_config_not_silent(self):
        data = binding_document()
        del data['fields']
        self.blocked(Code.CONFIG, lambda: save_binding(self.root, data))
        self.assertFalse((self.root / 'binding.json').exists())

    def test_missing_entry_fields_is_config_not_fallback(self):
        data = binding_document()
        del data['entry_fields']
        self.blocked(Code.CONFIG, lambda: save_binding(self.root, data))
        self.assertFalse((self.root / 'binding.json').exists())

    def test_missing_apply_fields_is_config_not_fallback(self):
        data = binding_document()
        del data['apply_fields']
        self.blocked(Code.CONFIG, lambda: save_binding(self.root, data))
        self.assertFalse((self.root / 'binding.json').exists())

    def test_missing_return_form_fields_is_config_not_fallback(self):
        data = binding_document()
        del data['return_form_fields']
        self.blocked(Code.CONFIG, lambda: save_binding(self.root, data))
        self.assertFalse((self.root / 'binding.json').exists())

    def test_return_item_key_is_optional_and_absent_means_unbound(self):
        # #77：归还表单的「归还物品」这一格可以不绑。缺键 = 不绑，不是配置错误。
        data = binding_document()
        del data['return_form_fields']['item']
        _, _, _, _, _, return_form_fields = binding_from_document(data)
        self.assertEqual(return_form_fields.item, '')
        self.assertEqual(return_form_fields.borrower,
                         SYNTHETIC_RETURN_FORM_FIELDS.borrower)

    def test_return_item_key_bound_to_a_non_field_id_is_config(self):
        # 绑了但值不是字段 ID：不能读成「没绑」，否则静默丢掉物品这一层收窄。
        data = binding_document()
        data['return_form_fields']['item'] = 7
        self.blocked(Code.CONFIG, lambda: save_binding(self.root, data))
        self.assertFalse((self.root / 'binding.json').exists())

    def test_entry_fields_cannot_reuse_ledger_map(self):
        data = binding_document()
        data['entry_fields'] = dict(data['fields'])
        self.blocked(Code.CONFIG, lambda: save_binding(self.root, data))

    def test_incomplete_field_map_is_config(self):
        data = binding_document()
        del data['fields']['return_id']
        self.blocked(Code.CONFIG, lambda: save_binding(self.root, data))

    def test_check_binding_uses_ledger_scope_not_item_id(self):
        binding, _entry = save_binding(self.root, binding_document())
        fixtures = _fixtures()
        current = fixtures.loan()
        check_binding(binding, current)
        other_item = replace(current.item, resource_id='synthetic-other-item')
        check_binding(binding, replace(current, item=other_item))
        other_ledger = replace(current.item, container_id='synthetic-other-table')
        self.blocked(Code.WRONG_LOAN, lambda: check_binding(binding, replace(current, item=other_ledger)))


class IntakeWatermarkTests(unittest.TestCase):
    """``application_intake.since``（#78 第 2 条）：配了就读出来，配错是 CONFIG。

    「配错」绝不能退回「没配」那个保守默认 —— 那样操作员会以为已经启用自动发现。
    """

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_absent_watermark_is_no_watermark(self):
        self.assertIsNone(intake_since_from_document({}))
        self.assertIsNone(intake_since_from_document({'application_intake': {}}))

    def test_the_watermark_is_read_back_timezone_aware(self):
        since = intake_since_from_document(
            binding_document(application_intake={'since': '2029-12-30T08:00:00+08:00'}))
        self.assertEqual(since,
                         datetime(2029, 12, 30, 8, 0, tzinfo=timezone(timedelta(hours=8))))
        self.assertIsNotNone(since.utcoffset())

    def test_a_broken_watermark_is_config_not_silently_unset(self):
        for document in (
                {'application_intake': 'now'},
                {'application_intake': {'since': 'not-a-time'}},
                {'application_intake': {'since': '2029-12-30T08:00:00'}},
                {'application_intake': {'since': '  2029-12-30T08:00:00+08:00  '}},
                {'application_intake': {'since': 123}},
                {'application_intake': {'since_at': '2029-12-30T08:00:00+08:00'}}):
            self.blocked(Code.CONFIG,
                         lambda document=document: intake_since_from_document(document))

    def test_the_binding_document_still_loads_with_the_extra_key(self):
        document = binding_document(
            application_intake={'since': '2029-12-30T08:00:00+08:00'})
        binding, _application, *_rest = binding_from_document(document)
        self.assertEqual(binding.config_version, 'synthetic-config-v1')
        self.assertIsNotNone(intake_since_from_document(document))
