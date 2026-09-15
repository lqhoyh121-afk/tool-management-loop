import importlib.util
from dataclasses import replace
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_memory_transport import MemoryTransport

from contracts.flow import plan, verify
from contracts.model import Action, Code, ContractError, Outcome, State
from contracts.ports import LedgerScope, RuntimeBinding, StageRequest, stage_operation_id
from integrations.dingtalk.adapter import DingTalkAdapter
from integrations.dingtalk.codec import encode_inventory, encode_loan
from integrations.dingtalk.layout import SYNTHETIC_FIELDS, entry_fields_from


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t03_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t03_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
loan, stock, event = _fixtures.loan, _fixtures.stock, _fixtures.event
MANAGER, ITEM, BORROWER, LOAN, NOW = (
    _fixtures.MANAGER, _fixtures.ITEM, _fixtures.BORROWER, _fixtures.LOAN, _fixtures.NOW)
SyntheticJournal, SyntheticLease = _synthetic.SyntheticJournal, _synthetic.SyntheticLease


class PortTests(unittest.TestCase):
    def setUp(self):
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = entry_fields_from(self.fields)
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = MemoryTransport(self.fields, self.entry_fields)
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields)
        self.seed(loan(), stock())

    def seed(self, current, inventory):
        self.transport.seed_record(current.ref, encode_loan(current, self.fields))
        self.transport.seed_record(inventory.ref, encode_inventory(inventory, self.fields))

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_read_loan_and_inventory(self):
        self.assertEqual(self.adapter.read_loan(LOAN), loan())
        self.assertEqual(self.adapter.read_inventory(ITEM), stock())

    def test_approve_write_roundtrip(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(verify(intent, receipt).outcome, Outcome.VERIFIED)
        self.assertEqual(self.adapter.read_loan(LOAN).state, State.RESERVATION_PENDING)

    def test_timeout_retries_when_records_unchanged(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.transport.drop_once.append('record.update')
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.UNKNOWN)
        self.assertEqual(len(self.transport.writes_of('record.update')), 1)
        self.assertEqual(self.adapter.query(intent).outcome, Outcome.NOT_SENT)
        retried = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(verify(intent, retried).outcome, Outcome.VERIFIED)
        self.assertEqual(len(self.transport.writes_of('record.update')), 3)

    def test_partial_write_stays_unknown(self):
        approved = plan(loan(), event(Action.APPROVE), stock())
        self.adapter.submit(approved, self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        inventory = self.adapter.read_inventory(ITEM)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        intent = plan(current, reserve, inventory)
        self.transport.drop_after_updates = self.transport.update_count + 1
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.UNKNOWN)
        self.assertEqual(self.adapter.query(intent).outcome, Outcome.UNKNOWN)
        writes = len(self.transport.writes_of('record.update'))
        self.blocked(Code.UNKNOWN, lambda: self.adapter.submit(intent, self.binding, self.lease))
        self.assertEqual(len(self.transport.writes_of('record.update')), writes)

    def test_permission_denied_is_identity(self):
        self.transport.fail_codes['record.query'] = 'FORBIDDEN'
        self.blocked(Code.IDENTITY, lambda: self.adapter.read_loan(LOAN))
        self.transport.fail_codes = {'record.update': 'PERMISSION_DENIED'}
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.blocked(Code.IDENTITY, lambda: self.adapter.submit(intent, self.binding, self.lease))

    def test_rate_limit_is_unknown_not_empty_success(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.transport.fail_codes['record.update'] = 'RATE_LIMITED'
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.UNKNOWN)

    def test_stale_inventory_is_conflict(self):
        self.seed(loan(), replace(stock(), available=1, revision='synthetic-rev-stale'))
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.blocked(Code.CONFLICT, lambda: self.adapter.submit(intent, self.binding, self.lease))
        self.assertEqual(self.transport.update_count, 0)

    def test_lost_lease_is_blocked(self):
        self.leases.release(self.lease)
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.blocked(Code.INSTANCE, lambda: self.adapter.submit(intent, self.binding, self.lease))

    def test_duplicate_operation_payload_rejected(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.journal.prepare(intent)
        self.blocked(Code.OP_CONFLICT, lambda: self.journal.prepare(
            replace(intent, stock_before=replace(stock(), available=4))))

    def test_approve_form_event_and_reject_decision(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertEqual(receipt.source.kind, 'form')
        again = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(again.source, receipt.source)
        self.assertEqual(len(self.transport.writes_of('form.create')), 1)

        self.transport.complete_form(receipt.source.resource_id, 'agree', NOW.isoformat())
        agreed = self.adapter.read_event(loan(), receipt.source)
        self.assertEqual(agreed.action, Action.APPROVE)
        self.assertEqual(agreed.actor, MANAGER)
        self.assertTrue(agreed.verified)

        self.transport.complete_form(receipt.source.resource_id, 'reject', NOW.isoformat())
        rejected = self.adapter.read_event(loan(), receipt.source)
        self.assertEqual(rejected.action, Action.REJECT)

    def test_form_wrong_loan_is_rejected(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        key = ('synthetic-org', 'synthetic-forms', receipt.source.resource_id)
        self.transport.records[key][self.entry_fields.loan_id] = 'synthetic-other-loan'
        self.transport.complete_form(receipt.source.resource_id, 'agree', NOW.isoformat())
        self.blocked(Code.WRONG_LOAN, lambda: self.adapter.read_event(loan(), receipt.source))

    def test_issue_todo_binding_and_wrong_person(self):
        current, inventory = self._advance_to_issue()
        request = StageRequest(stage_operation_id(current, Action.ISSUE),
                               current, Action.ISSUE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertEqual(receipt.source.kind, 'todo')
        self.assertIsNotNone(receipt.binding)
        self.assertEqual(receipt.binding.contact, MANAGER)
        self.assertEqual(receipt.binding.internal.namespace, 'todo')

        self.transport.complete_todo(receipt.source.resource_id, NOW.isoformat(), creator_id=9000000999)
        self.blocked(Code.WRONG_PERSON,
                     lambda: self.adapter.read_event(current, receipt.source))

        self.transport.complete_todo(receipt.source.resource_id, NOW.isoformat())
        done = self.adapter.read_event(current, receipt.source)
        self.assertEqual(done.action, Action.ISSUE)
        self.assertEqual(done.event_id, f'{receipt.source.resource_id}:completion')
        self.assertEqual(done.actor, receipt.binding.internal)
        self.assertEqual(done.occurred_at, NOW)
        intent = plan(current, done, inventory)
        written = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(verify(intent, written).outcome, Outcome.VERIFIED)

    def test_todo_without_iso_time_is_not_converted_from_finish_time(self):
        current, _inventory = self._advance_to_issue()
        request = StageRequest(stage_operation_id(current, Action.ISSUE),
                               current, Action.ISSUE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        self.transport.complete_todo(receipt.source.resource_id, NOW.isoformat())
        self.transport.todos[receipt.source.resource_id]['occurred_at'] = None
        self.blocked(Code.EVIDENCE, lambda: self.adapter.read_event(current, receipt.source))

    def test_stage_container_comes_from_query_not_fixture_name(self):
        self.transport.form_container = 'deployed-collect-forms'
        self.transport.todo_container = 'deployed-manager-todos'
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        form = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(form.outcome, Outcome.VERIFIED)
        self.assertEqual(form.source.container_id, 'deployed-collect-forms')
        self.assertNotEqual(form.source.container_id, 'synthetic-forms')

        current, _inventory = self._advance_to_issue()
        issue = StageRequest(stage_operation_id(current, Action.ISSUE),
                             current, Action.ISSUE, MANAGER)
        todo = self.adapter.create_stage(issue, self.binding, self.lease)
        self.assertEqual(todo.outcome, Outcome.VERIFIED)
        self.assertEqual(todo.source.container_id, 'deployed-manager-todos')
        self.assertNotEqual(todo.source.container_id, 'synthetic-todos')

    def test_stage_query_without_container_is_unknown(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        form = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(form.outcome, Outcome.VERIFIED)
        del self.transport.stages[request.operation_id]['container']
        queried = self.adapter.query_stage(request)
        self.assertEqual(queried.outcome, Outcome.UNKNOWN)
        self.assertIsNone(queried.source)

    def test_stage_timeout_queries_without_recreate(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        self.transport.drop_once.append('form.create')
        first = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(first.outcome, Outcome.UNKNOWN)
        self.assertEqual(len(self.transport.writes_of('form.create')), 1)
        second = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(second.outcome, Outcome.UNKNOWN)
        self.assertEqual(len(self.transport.writes_of('form.create')), 1)

    def test_whole_chain_through_injected_transport(self):
        current, inventory = loan(), stock()
        request = StageRequest(stage_operation_id(current, Action.APPROVE),
                               current, Action.APPROVE, MANAGER)
        form = self.adapter.create_stage(request, self.binding, self.lease)
        self.transport.complete_form(form.source.resource_id, 'agree', NOW.isoformat())
        approved = self.adapter.read_event(current, form.source)
        intent = plan(current, approved, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current, inventory = verify(intent, receipt).loan, receipt.inventory

        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        intent = plan(current, reserve, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current, inventory = verify(intent, receipt).loan, receipt.inventory

        issue_req = StageRequest(stage_operation_id(current, Action.ISSUE),
                                 current, Action.ISSUE, MANAGER)
        todo = self.adapter.create_stage(issue_req, self.binding, self.lease)
        self.transport.complete_todo(todo.source.resource_id, NOW.isoformat())
        issued = self.adapter.read_event(current, todo.source)
        intent = plan(current, issued, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current, inventory = verify(intent, receipt).loan, receipt.inventory

        return_req = StageRequest(stage_operation_id(current, Action.REQUEST_RETURN),
                                  current, Action.REQUEST_RETURN, BORROWER)
        ret_form = self.adapter.create_stage(return_req, self.binding, self.lease)
        self.transport.complete_form(
            ret_form.source.resource_id, 'return', NOW.isoformat(),
            return_container='synthetic-returns', return_id='synthetic-return',
            quantity=2, physical_ids=())
        requested = self.adapter.read_event(current, ret_form.source)
        intent = plan(current, requested, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current, inventory = verify(intent, receipt).loan, receipt.inventory

        close_req = StageRequest(stage_operation_id(current, Action.RETURN),
                                 current, Action.RETURN, MANAGER)
        close_todo = self.adapter.create_stage(close_req, self.binding, self.lease)
        self.assertNotEqual(close_todo.source.resource_id, todo.source.resource_id)
        self.transport.complete_todo(close_todo.source.resource_id, NOW.isoformat())
        returned = self.adapter.read_event(current, close_todo.source)
        intent = plan(current, returned, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(verify(intent, receipt).outcome, Outcome.VERIFIED)
        self.assertEqual((receipt.loan.state, receipt.inventory.available,
                          receipt.inventory.borrowed),
                         (State.CLOSED, 5, 0))

    def _advance_to_issue(self):
        current, inventory = loan(), stock()
        intent = plan(current, event(Action.APPROVE), inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current, inventory = verify(intent, receipt).loan, receipt.inventory
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        intent = plan(current, reserve, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        return verify(intent, receipt).loan, receipt.inventory


if __name__ == '__main__':
    unittest.main()
