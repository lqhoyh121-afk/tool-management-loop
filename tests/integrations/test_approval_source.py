"""审批真源与审批人催办待办（issue #57 剩余两项）。

真源只有一处：阶段入口行的「决定」列（`_read_entry_form_event`）。待审批阶段同时给
审批人发一条待办，但它只是催办 —— 点没点完成都读不出、也改不了结论。
"""
import importlib.util
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_layout import entry_fields_from
from t03_memory_transport import MemoryTransport

from contracts.model import (Action, Code, ContractError, Identity, Outcome,
                             Resource)
from contracts.ports import (LedgerScope, RuntimeBinding, StageRequest,
                             stage_operation_id)
from integrations.dingtalk.adapter import DingTalkAdapter, stage_title
from integrations.dingtalk.codec import encode_inventory, encode_loan
from integrations.dingtalk.layout import SYNTHETIC_FIELDS


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t57_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t57_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
loan, stock = _fixtures.loan, _fixtures.stock
MANAGER, ITEM, LOAN, NOW = (_fixtures.MANAGER, _fixtures.ITEM, _fixtures.LOAN,
                            _fixtures.NOW)
SyntheticJournal, SyntheticLease = _synthetic.SyntheticJournal, _synthetic.SyntheticLease

APPROVER = Identity('contact', 'synthetic-org', 'synthetic-approver')
TODO_CONTAINER = 'synthetic-todos'


def approved_loan():
    """审批人与管理人是两个人，才看得出催办待办的收件人是谁。"""
    return replace(loan(), approver=APPROVER)


class ApprovalSourceTests(unittest.TestCase):
    def setUp(self):
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = entry_fields_from(self.fields)
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.current = approved_loan()
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            APPROVER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = MemoryTransport(self.fields, self.entry_fields)
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields)
        self.transport.seed_record(self.current.ref,
                                   encode_loan(self.current, self.fields))
        self.transport.seed_record(stock().ref,
                                   encode_inventory(stock(), self.fields))

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def create_approve_stage(self):
        request = StageRequest(stage_operation_id(self.current, Action.APPROVE),
                               self.current, Action.APPROVE, APPROVER)
        return request, self.adapter.create_stage(request, self.binding, self.lease)

    def nudge_task_id(self):
        self.assertEqual(len(self.transport.by_task), 1)
        return next(iter(self.transport.by_task))

    def test_approve_stage_sends_one_todo_to_the_approver(self):
        request, receipt = self.create_approve_stage()
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertEqual(receipt.source.kind, 'form')

        sends = self.transport.writes_of('todo.create')
        self.assertEqual(len(sends), 1)
        arguments = sends[0][1]
        self.assertEqual(arguments['actor'], APPROVER.user_id)
        self.assertNotEqual(arguments['actor'], self.current.manager.user_id)
        self.assertEqual(arguments['title'], stage_title(self.current, Action.APPROVE))
        self.assertIn('待审批', arguments['title'])
        self.assertIn(self.current.ref.resource_id, arguments['title'])

        # 催办另用一个操作号：阶段索引里这个操作号仍指向入口行（真源）。
        self.assertEqual(self.transport.stages[request.operation_id]['kind'], 'form')
        self.assertEqual(len(self.transport.stages), 2)

    def test_approve_stage_is_created_and_nudged_once(self):
        request, receipt = self.create_approve_stage()
        again = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(again.source, receipt.source)
        self.assertEqual(len(self.transport.writes_of('form.create')), 1)
        self.assertEqual(len(self.transport.writes_of('todo.create')), 1)

    def test_approve_truth_is_the_entry_decision_column(self):
        request, receipt = self.create_approve_stage()
        form_id = receipt.source.resource_id
        task_id = self.nudge_task_id()

        # 审批人把催办待办点了完成，入口行的「决定」列还空着：读不出结论。
        self.transport.complete_todo(task_id, NOW)
        self.blocked(Code.EVIDENCE,
                     lambda: self.adapter.read_event(self.current, receipt.source))

        # 决定列填「拒绝」：事件读得出来，actor 是审批人。
        self.transport.complete_form(form_id, '拒绝', NOW.isoformat())
        rejected = self.adapter.read_event(self.current, receipt.source)
        self.assertEqual(rejected.action, Action.REJECT)
        self.assertEqual(rejected.actor, APPROVER)
        self.assertEqual(rejected.source, receipt.source)
        self.assertEqual(rejected.evidence_kind, 'form')

        # 同一行的决定列改成「同意」：读出同意。真源就是这一列。
        self.transport.complete_form(form_id, '同意', NOW.isoformat())
        agreed = self.adapter.read_event(self.current, receipt.source)
        self.assertEqual(agreed.action, Action.APPROVE)

        # 词表以外的写法一律 fail closed，不从别处猜。
        self.transport.complete_form(form_id, 'maybe', NOW.isoformat())
        self.blocked(Code.EVIDENCE,
                     lambda: self.adapter.read_event(self.current, receipt.source))

    def test_completed_nudge_todo_is_not_an_approval_event(self):
        _request, receipt = self.create_approve_stage()
        task_id = self.nudge_task_id()
        self.transport.complete_todo(task_id, NOW)
        nudge = Resource('todo', LOAN.tenant_id, TODO_CONTAINER, task_id)
        # 催办待办完成只能是 ISSUE/RETURN 那种阶段证据，审批结论只认入口行。
        self.blocked(Code.STATE, lambda: self.adapter.read_event(self.current, nudge))


if __name__ == '__main__':
    unittest.main()
