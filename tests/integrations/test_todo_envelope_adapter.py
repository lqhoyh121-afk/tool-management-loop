"""Todo channel envelope vs aitable: create_stage must not crash; read must not fake EVIDENCE."""
import importlib.util
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_memory_transport import MemoryTransport, error_envelope, ok_envelope

from contracts.flow import plan, verify
from contracts.model import Action, Code, ContractError, Outcome, State
from contracts.ports import LedgerScope, RuntimeBinding, StageRequest, stage_operation_id
from integrations.dingtalk.adapter import DingTalkAdapter
from integrations.dingtalk.codec import encode_inventory, encode_loan
from integrations.dingtalk.layout import SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_FIELDS


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t34_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t34_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
loan, stock, event = _fixtures.loan, _fixtures.stock, _fixtures.event
MANAGER, ITEM, LOAN, NOW = (
    _fixtures.MANAGER, _fixtures.ITEM, _fixtures.LOAN, _fixtures.NOW)
SyntheticJournal, SyntheticLease = _synthetic.SyntheticJournal, _synthetic.SyntheticLease


class AitableTodoTransport(MemoryTransport):
    """Returns aitable-shaped envelopes for todo commands (pre-#34 live mismatch)."""

    def exchange(self, command, arguments):
        if command == 'todo.create':
            self.calls.append((command, dict(arguments)))
            return ok_envelope(result={
                'taskId': 'SYNTHETIC-todo-bad-envelope',
                'todoDetailModel': {
                    'taskId': 'SYNTHETIC-todo-bad-envelope',
                    'isDone': False,
                    'finishTime': 0,
                    'executorIds': [9000000100],
                    'activities': [],
                },
            })
        if command == 'todo.get':
            self.calls.append((command, dict(arguments)))
            return ok_envelope(result={
                'todoDetailModel': {
                    'taskId': arguments['task_id'],
                    'isDone': True,
                    'finishTime': int(NOW.timestamp() * 1000),
                    'executorIds': [9000000100],
                    'activities': [
                        {'activityId': 'SYN-a', 'action': 'task.done', 'creatorId': 9000000100},
                    ],
                },
            })
        return super().exchange(command, arguments)


class TodoEnvelopeAdapterTests(unittest.TestCase):
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

    def test_create_stage_todo_live_envelope_does_not_crash(self):
        approved = plan(loan(), event(Action.APPROVE), stock())
        self.adapter.submit(approved, self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        inventory = self.adapter.read_inventory(ITEM)
        self.adapter.submit(plan(current, reserve, inventory), self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        request = StageRequest(stage_operation_id(current, Action.ISSUE),
                               current, Action.ISSUE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertEqual(receipt.source.kind, 'todo')

    def _seed(self, transport):
        transport.seed_record(loan().ref, encode_loan(loan(), self.fields))
        transport.seed_record(stock().ref, encode_inventory(stock(), self.fields))

    def test_create_stage_aitable_shaped_todo_returns_unknown_not_crash(self):
        bad_transport = AitableTodoTransport(self.fields, self.entry_fields)
        self._seed(bad_transport)
        bad = DingTalkAdapter(
            bad_transport, self.journal, self.leases, self.fields, self.entry_fields)
        approved = plan(loan(), event(Action.APPROVE), stock())
        bad.submit(approved, self.binding, self.lease)
        current = bad.read_loan(LOAN)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        inventory = bad.read_inventory(ITEM)
        bad.submit(plan(current, reserve, inventory), self.binding, self.lease)
        current = bad.read_loan(LOAN)
        request = StageRequest(stage_operation_id(current, Action.ISSUE),
                               current, Action.ISSUE, MANAGER)
        receipt = bad.create_stage(request, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.UNKNOWN)
        self.assertEqual(len(bad.transport.writes_of('todo.create')), 1)

    def test_read_todo_event_aitable_shaped_get_is_unknown_not_evidence(self):
        approved = plan(loan(), event(Action.APPROVE), stock())
        self.adapter.submit(approved, self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        inventory = self.adapter.read_inventory(ITEM)
        self.adapter.submit(plan(current, reserve, inventory), self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        request = StageRequest(stage_operation_id(current, Action.ISSUE),
                               current, Action.ISSUE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        bad_transport = AitableTodoTransport(self.fields, self.entry_fields)
        self._seed(bad_transport)
        bad = DingTalkAdapter(
            bad_transport, self.journal, self.leases, self.fields, self.entry_fields)
        self.blocked(Code.UNKNOWN, lambda: bad.read_event(current, receipt.source))

    def test_completed_todo_reads_back_with_live_envelope(self):
        approved = plan(loan(), event(Action.APPROVE), stock())
        self.adapter.submit(approved, self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        inventory = self.adapter.read_inventory(ITEM)
        self.adapter.submit(plan(current, reserve, inventory), self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        request = StageRequest(stage_operation_id(current, Action.ISSUE),
                               current, Action.ISSUE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        self.transport.complete_todo(receipt.source.resource_id, NOW.isoformat())
        event_read = self.adapter.read_event(current, receipt.source)
        self.assertEqual(event_read.action, Action.ISSUE)

    def test_issue_todo_without_result_occurred_at_reaches_borrowed(self):
        """Live todo.get has finishTime/activities but no result.occurredAt (#47)."""
        approved = plan(loan(), event(Action.APPROVE), stock())
        self.adapter.submit(approved, self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        inventory = self.adapter.read_inventory(ITEM)
        reserve_receipt = self.adapter.submit(
            plan(current, reserve, inventory), self.binding, self.lease)
        current = self.adapter.read_loan(LOAN)
        inventory = reserve_receipt.inventory
        request = StageRequest(stage_operation_id(current, Action.ISSUE),
                               current, Action.ISSUE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        self.transport.complete_todo(receipt.source.resource_id, NOW.isoformat())
        payload = self.transport.exchange('todo.get', {
            'tenant_id': receipt.source.tenant_id,
            'task_id': receipt.source.resource_id,
        })
        result = payload.get('result')
        self.assertIsInstance(result, dict)
        self.assertNotIn('occurredAt', result)
        self.assertIn('todoDetailModel', result)
        done = self.adapter.read_event(current, receipt.source)
        intent = plan(current, done, inventory)
        written = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(verify(intent, written).outcome, Outcome.VERIFIED)
        self.assertEqual(written.loan.state, State.BORROWED)


if __name__ == '__main__':
    unittest.main()
