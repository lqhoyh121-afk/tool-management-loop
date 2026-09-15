"""Two containers, two maps: overlapping keys must not be interchangeable."""
import importlib.util
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_layout import entry_fields_from
from t03_memory_transport import MemoryTransport

from contracts.model import Action, Code, ContractError
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
_fixtures = _load('t25_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t25_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
loan, stock = _fixtures.loan, _fixtures.stock
MANAGER, ITEM, LOAN, NOW = (
    _fixtures.MANAGER, _fixtures.ITEM, _fixtures.LOAN, _fixtures.NOW)
SyntheticJournal, SyntheticLease = _synthetic.SyntheticJournal, _synthetic.SyntheticLease

_OVERLAP = (
    'config_version', 'quantity', 'physical_ids', 'borrower', 'approver',
    'manager', 'return_container', 'return_id',
)


class SplitFieldMapTests(unittest.TestCase):
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

    def test_eight_overlapping_keys_use_different_synthetic_ids(self):
        for name in _OVERLAP:
            self.assertNotEqual(getattr(self.fields, name), getattr(self.entry_fields, name), name)

    def test_encode_loan_writes_ledger_ids_not_entry_ids(self):
        cells = encode_loan(loan(), self.fields)
        self.assertNotIn(self.entry_fields.return_id, cells)
        self.assertNotIn(self.fields.return_id, cells)
        self.assertEqual(decode_loan(LOAN, cells, self.fields), loan())

    def test_decode_loan_rejects_entry_ids_on_ledger_cells(self):
        cells = encode_loan(loan(), self.fields)
        confused = replace(self.fields, **{
            name: getattr(self.entry_fields, name) for name in _OVERLAP
        })
        self.blocked(Code.EVIDENCE, lambda: decode_loan(LOAN, cells, confused))

    def test_read_loan_uses_ledger_map(self):
        self.assertEqual(self.adapter.read_loan(LOAN), loan())

    def test_form_create_and_read_use_entry_map(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        key = ('synthetic-org', 'synthetic-forms', receipt.source.resource_id)
        cells = self.transport.records[key]
        self.assertIn(self.entry_fields.config_version, cells)
        self.assertNotIn(self.fields.config_version, cells)
        self.transport.complete_form(receipt.source.resource_id, 'agree', NOW.isoformat())
        agreed = self.adapter.read_event(loan(), receipt.source)
        self.assertEqual(agreed.action, Action.APPROVE)

    def test_form_cells_keyed_by_ledger_ids_are_not_events(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        key = ('synthetic-org', 'synthetic-forms', receipt.source.resource_id)
        cells = self.transport.records[key]
        for name in _OVERLAP:
            entry_id = getattr(self.entry_fields, name)
            ledger_id = getattr(self.fields, name)
            if entry_id in cells:
                cells[ledger_id] = cells.pop(entry_id)
        self.transport.complete_form(receipt.source.resource_id, 'agree', NOW.isoformat())
        self.blocked(Code.EVIDENCE, lambda: self.adapter.read_event(loan(), receipt.source))

    def test_shared_table_helper_keeps_existing_single_map_tests(self):
        shared = entry_fields_from(self.fields)
        self.assertEqual(shared.return_id, self.fields.return_id)
        self.assertNotEqual(shared.return_id, self.entry_fields.return_id)

    def test_entry_fields_from_is_not_in_production_layout(self):
        layout = Path(__file__).resolve().parents[2] / 'integrations' / 'dingtalk' / 'layout.py'
        source = layout.read_text(encoding='utf-8')
        self.assertNotIn('def entry_fields_from', source)


if __name__ == '__main__':
    unittest.main()
