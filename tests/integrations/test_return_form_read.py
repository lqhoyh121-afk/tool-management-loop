"""Return form rows: borrower + return time only (#49)."""
import importlib.util
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_memory_transport import MemoryTransport

from contracts.flow import plan, verify
from contracts.model import Action, Code, ContractError, Outcome, Resource, State
from contracts.ports import LedgerScope, RuntimeBinding
from integrations.dingtalk.adapter import DingTalkAdapter
from integrations.dingtalk.codec import _put_identity, encode_inventory, encode_loan
from integrations.dingtalk.layout import (
    SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_FIELDS, SYNTHETIC_RETURN_FORM_FIELDS)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t49_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t49_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
loan, stock = _fixtures.loan, _fixtures.stock
MANAGER, BORROWER, ITEM, LOAN, NOW = (
    _fixtures.MANAGER, _fixtures.BORROWER, _fixtures.ITEM, _fixtures.LOAN, _fixtures.NOW)
SyntheticJournal, SyntheticLease = _synthetic.SyntheticJournal, _synthetic.SyntheticLease

ENTRY_CONTAINER = 'synthetic-forms'
LOAN_CONTAINER = 'synthetic-loans'


def _live_select(name):
    return {'id': f'SYNTHETIC-rand-{name}', 'name': name}


def _borrowed_loan():
    current = replace(loan(), state=State.BORROWED)
    cells = encode_loan(current, SYNTHETIC_FIELDS)
    cells[SYNTHETIC_FIELDS.state] = _live_select(State.BORROWED.value)
    cells[SYNTHETIC_FIELDS.tracked] = _live_select('false')
    return current, cells


class ReturnFormReadTests(unittest.TestCase):
    def setUp(self):
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = SYNTHETIC_ENTRY_FIELDS
        self.return_form_fields = SYNTHETIC_RETURN_FORM_FIELDS
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = MemoryTransport(self.fields, self.entry_fields)
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            return_form_fields=self.return_form_fields,
            entry_container=ENTRY_CONTAINER, loan_container=LOAN_CONTAINER)
        borrowed, cells = _borrowed_loan()
        self.borrowed = borrowed
        self.transport.seed_record(borrowed.ref, cells)
        self.transport.seed_record(stock().ref, encode_inventory(stock(), self.fields))

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def _seed_return_row(self, form_id='synthetic-return-form-row'):
        cells = {
            self.return_form_fields.borrower: _put_identity(BORROWER),
            self.return_form_fields.occurred_at: NOW.isoformat(),
        }
        source = Resource('form', 'synthetic-org', ENTRY_CONTAINER, form_id)
        self.transport.seed_record(source, cells)
        return source

    def test_minimal_return_form_row_reads_request_return(self):
        source = self._seed_return_row()
        event = self.adapter.read_event(self.borrowed, source)
        self.assertEqual(event.action, Action.REQUEST_RETURN)
        self.assertEqual(event.actor, BORROWER)
        self.assertEqual(event.return_ref, Resource(
            'record', 'synthetic-org', ENTRY_CONTAINER, source.resource_id))
        intent = plan(self.borrowed, event, stock())
        self.assertEqual(intent.after.state, State.AWAITING_RETURN)

    def test_resolve_return_form_loan_ignores_wrong_hint(self):
        source = self._seed_return_row()
        wrong = replace(self.borrowed.ref, resource_id='synthetic-other-loan')
        resolved = self.adapter.resolve_return_form_loan(source, wrong)
        self.assertEqual(resolved, self.borrowed.ref)

    def test_two_borrowed_loans_for_same_borrower_blocks(self):
        other = replace(self.borrowed, ref=replace(
            self.borrowed.ref, resource_id='synthetic-loan-2'))
        cells = encode_loan(other, self.fields)
        cells[self.fields.state] = _live_select(State.BORROWED.value)
        cells[self.fields.tracked] = _live_select('false')
        self.transport.seed_record(other.ref, cells)
        source = self._seed_return_row()
        self.blocked(Code.EVIDENCE, lambda: self.adapter.read_event(self.borrowed, source))

    def test_engine_precreated_row_still_uses_entry_map(self):
        cells = {
            self.entry_fields.loan_container: LOAN.container_id,
            self.entry_fields.loan_id: LOAN.resource_id,
            self.entry_fields.config_version: self.borrowed.config_version,
            self.entry_fields.decision: _live_select('return'),
            self.entry_fields.borrower: _put_identity(BORROWER),
            self.entry_fields.approver: _put_identity(MANAGER),
            self.entry_fields.manager: _put_identity(MANAGER),
            self.entry_fields.quantity: '2',
            self.entry_fields.physical_ids: '[]',
            self.entry_fields.occurred_at: NOW.isoformat(),
            self.entry_fields.return_container: 'synthetic-returns',
            self.entry_fields.return_id: 'synthetic-return-record',
            self.entry_fields.action: 'request_return',
            self.entry_fields.operation_id: 'synthetic-op',
        }
        source = Resource('form', 'synthetic-org', ENTRY_CONTAINER, 'synthetic-engine-row')
        self.transport.seed_record(source, cells)
        event = self.adapter.read_event(self.borrowed, source)
        self.assertEqual(event.action, Action.REQUEST_RETURN)
        self.assertEqual(event.return_ref.resource_id, 'synthetic-return-record')


if __name__ == '__main__':
    unittest.main()
