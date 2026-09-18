"""阶段待办完成事件 → 引擎一轮内消费，并在报告里点名（issue #93）。

真机实测（2026-09-18）：真人点了【待领用确认】待办（`isDone=true`、`activities` 里有
`task.done`/`task.self.done`、`finishTime` 有值），引擎**其实消费了**它 —— 台账从
`awaiting_issue_confirmation` 推到 `borrowed`，同一轮里连下一阶段入口都建了。但那一轮报告里
除了一行「处理 1」之外**一个字都没有**：跳过行只印跳过的，已处理的条目不点名。于是这次正常
推进被报成了「引擎漏消费、事件根本没进扫描面」，一整条「点待办即证据」的主链被当成硬阻断。

这个文件把这条链钉在**真传输层**上（`DwsTransport` + 子进程替身，报文形态照真机观测的
`todoDetailModel` 写），而不是内存传输层，逐环覆盖：

1. 事件进得了扫描面：阶段条目由 `_stage_items` 从操作日志取（不是队列喂进来的）；
2. 完成判定：`isDone` + `task.self.done` 活动 + 毫秒 `finishTime` → 认成完成事件；
3. 阶段索引回查、迁移、库存移动，一轮内完成，台账 event id 落进 `consumed_events`；
4. 报告点名：摘要的「处理 N」与「处理 …」明细行一一对应，行里带单号、待办 id 与状态迁移；
5. 回归：同一条完成事件再跑一轮**不再消费**（不重复写台账、不重复迁库存、不多推待办）；
6. 待办没点/不是执行人点的：既不算完成，也不出现在「处理」行里（反向判据）；
7. 回包丢了、索引里只剩标题时：按**发出去的那份标题**（且两状态都查）认领，同一轮里消费。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t031_fake_dws import load_state, save_state
from t03_layout import entry_fields_from
from t03_live_cells import declared_kinds

from bootstrap.drive import (DriveLoop, StaticSources, format_drive_lines)
from bootstrap.instance import MachineLock
from bootstrap.journal import FileJournal
from contracts.model import Action, Code, Resource, State
from contracts.ports import (LedgerScope, RuntimeBinding, stage_operation_id)
from integrations.dingtalk.adapter import DingTalkAdapter, TitleDisplay
from integrations.dingtalk.codec import encode_inventory, encode_loan
from integrations.dingtalk.dws_transport import DwsTransport
from integrations.dingtalk.layout import SYNTHETIC_FIELDS
from workflow.engine import LendingEngine


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t93_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
loan, stock = _fixtures.loan, _fixtures.stock
MANAGER = _fixtures.MANAGER
NOW = _fixtures.NOW

LOAN = Resource('record', 'synthetic-org', 'baseLoan/tblLoan', 'recLoan')
ITEM = Resource('record', 'synthetic-org', 'baseStock/tblStock', 'recItem')
FORM_CONTAINER = 'baseForm/tblForm'
TODO_CONTAINER = 'todoSpace/executors'
ITEM_NAME_FIELD = 'fldSYN-item-name'
ITEM_NAME = 'SYN-万用表'
BORROWER_NAME = 'SYN-张三'
# 真机上点过的那条待办的活动形态：task.create + task.done + task.self.done，
# creatorId 是待办内部人员 ID（int），finishTime 是毫秒。
FAKE_DWS = Path(__file__).resolve().parent / 't031_fake_dws.py'


class StageCompletionDriveTests(unittest.TestCase):
    """一笔停在「待领用确认」的单：阶段待办被点完成 → 一轮内推进到已借出。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.state_path = self.work / 'fake-state.json'
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = entry_fields_from(self.fields)
        state = load_state(self.state_path)
        state['kinds'] = declared_kinds(self.fields, self.entry_fields)
        save_state(self.state_path, state)

        self.current = replace(loan(), ref=LOAN, item=ITEM, state=State.AWAITING_ISSUE)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.runtime = self.work / 'runtime'
        self.journal = FileJournal(self.runtime / 'operations')
        self.locks = MachineLock(self.work / 'locks')
        self.transport = DwsTransport(
            [sys.executable, str(FAKE_DWS)], self.fields,
            form_container=FORM_CONTAINER, todo_container=TODO_CONTAINER,
            work_dir=self.runtime, entry_fields=self.entry_fields,
            extra_env={'FAKE_DWS_STATE': str(self.state_path)},
        )
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.locks, self.fields, self.entry_fields,
            title_display=TitleDisplay(ITEM_NAME_FIELD, True))
        self.engine = LendingEngine(self.adapter, self.adapter, self.adapter,
                                    self.journal, self.locks, self.binding)
        self.engine.start(self.binding.ledger, self.binding.account)
        self.issue_operation = stage_operation_id(self.current, Action.ISSUE)
        self.seed(self.current, replace(stock(), ref=ITEM, available=3, reserved=2),
                  item_name=ITEM_NAME, borrower_name=BORROWER_NAME)

    def tearDown(self):
        try:
            self.engine.stop()
        finally:
            self.temp.cleanup()

    # ---- 真机形态的本机替身 ----

    def seed(self, current, inventory, item_name=None, borrower_name=None):
        """把台账行、库存行、物品名称与通讯录写成真机读得到的样子。"""
        state = load_state(self.state_path)
        state['records'][f'{current.ref.container_id}/{current.ref.resource_id}'] = (
            encode_loan(current, self.fields))
        cells = encode_inventory(inventory, self.fields)
        if item_name is not None:
            cells[ITEM_NAME_FIELD] = item_name
        state['records'][f'{inventory.ref.container_id}/{inventory.ref.resource_id}'] = cells
        state['login_user'] = MANAGER.user_id
        state['contact_names'] = ({current.borrower.user_id: borrower_name}
                                  if borrower_name else {})
        save_state(self.state_path, state)

    def forget_display_names(self):
        """显示名此刻查不到：现算标题会与发出去的那份不同（回查的键必须仍是发出那份）。"""
        state = load_state(self.state_path)
        state['contact_names'] = {}
        cells = state['records'][f'{ITEM.container_id}/{ITEM.resource_id}']
        cells.pop(ITEM_NAME_FIELD, None)
        save_state(self.state_path, state)

    def complete(self, task_id, actor=None):
        """真人点了待办：isDone + 完成活动 + 毫秒 finishTime（真机观测到的形态）。"""
        state = load_state(self.state_path)
        detail = state['todos'][task_id]['detail']
        internal = detail['executorIds'][0]
        creator = internal if actor is None else actor
        detail['isDone'] = True
        detail['finishTime'] = int(NOW.timestamp() * 1000)
        detail['activities'] = [
            {'activityId': 'SYN-a-create', 'action': 'task.create', 'creatorId': internal},
            {'activityId': 'SYN-a-done', 'action': 'task.done', 'creatorId': creator},
            {'activityId': 'SYN-a-self-done', 'action': 'task.self.done', 'creatorId': creator},
        ]
        save_state(self.state_path, state)

    def issue_stage(self):
        """引擎自己的阶段入口：建出【待领用确认】待办并记进操作日志（生产路径）。"""
        receipt = self.engine.ensure_stage(self.current, Action.ISSUE)
        self.assertEqual(receipt.outcome.value, 'verified')
        self.assertEqual(receipt.source.kind, 'todo')
        return receipt

    def issue_stage_losing_the_reply(self):
        """待办真的建出来了，但回包丢了：索引里只剩标题，没有 task id（#72 的形态）。"""
        state = load_state(self.state_path)
        state['late_write'] = ['todo task create']
        save_state(self.state_path, state)
        self.transport._login_user_id()       # 正常超时下先观察登录账号
        self.transport.timeout = 0.3
        try:
            return self.engine.ensure_stage(self.current, Action.ISSUE)
        finally:
            self.transport.timeout = 30
            state = load_state(self.state_path)
            state['late_write'] = []
            save_state(self.state_path, state)

    def drive(self):
        return DriveLoop(self.engine, StaticSources(()), self.journal, self.locks).run()

    def live_loan(self):
        return self.adapter.read_loan(LOAN)

    def stock_counts(self):
        current = self.adapter.read_inventory(ITEM)
        return (current.available, current.reserved, current.borrowed)

    def todos(self):
        return load_state(self.state_path)['todos']

    def indexed(self, operation_id):
        store = json.loads(
            (self.runtime / 'dws-stage-index.json').read_text(encoding='utf-8'))
        return store['by_operation'].get(operation_id)

    def todo_skips(self, report):
        return [o for o in report.skipped if o.source_kind == 'todo']

    # ---- 1+4：一轮内消费，且报告点名 ----

    def test_a_completed_stage_todo_is_consumed_in_one_pass_and_named(self):
        receipt = self.issue_stage()
        task_id = receipt.source.resource_id
        self.assertEqual(self.live_loan().state, State.AWAITING_ISSUE)
        self.assertEqual(self.indexed(self.issue_operation)['title'],
                         self.todos()[task_id]['subject'])
        self.complete(task_id)

        report = self.drive()

        # 一轮内推进：台账已借出、库存从「预留」移到「借出」，下一步入口已建。
        self.assertEqual(self.live_loan().state, State.BORROWED)
        self.assertEqual(self.live_loan().consumed_events,
                         (f'{task_id}:completion',))
        self.assertEqual(self.stock_counts(), (3, 0, 2))
        self.assertEqual(self.todo_skips(report), [])
        self.assertEqual(len(report.processed), 1)
        self.assertEqual(report.processed[0].source.resource_id, task_id)
        self.assertEqual(report.processed[0].action, Action.ISSUE.value)
        following = self.journal.load(stage_operation_id(self.live_loan(),
                                                         Action.REQUEST_RETURN))[1]
        self.assertEqual(following.outcome.value, 'verified')

        # 报告点名：这一条处理的是哪张单、哪条待办、从哪推到哪。
        named = [e for e in report.evidence if e.source_id == task_id]
        self.assertEqual(len(named), 1)
        self.assertEqual((named[0].kind, named[0].loan_id, named[0].source_kind),
                         ('event', LOAN.resource_id, 'todo'))
        self.assertEqual(named[0].before_state, State.AWAITING_ISSUE.value)
        self.assertEqual(named[0].after_state, State.BORROWED.value)
        lines = format_drive_lines(report)
        text = '\n'.join(lines)
        self.assertIn(f'  处理 event loan={LOAN.resource_id} todo={task_id} '
                      f'{Action.ISSUE.value} awaiting_issue_confirmation→borrowed', text)
        self.assertEqual(sum(1 for line in lines if line.startswith('  处理 ')),
                         len(report.processed))
        # 消费过的完成事件不再算「等人点」。
        self.assertNotIn('阶段待办未完成', text)

    # ---- 5：已消费过的完成事件不得被重复消费 ----

    def test_a_second_pass_does_not_consume_the_same_completion_again(self):
        receipt = self.issue_stage()
        task_id = receipt.source.resource_id
        self.complete(task_id)
        first = self.drive()
        self.assertEqual([e.source_id for e in first.evidence], [task_id])
        intents_after_first = self.journal.ids()

        second = self.drive()

        self.assertEqual(self.live_loan().state, State.BORROWED)
        self.assertEqual(self.live_loan().consumed_events, (f'{task_id}:completion',))
        self.assertEqual(self.stock_counts(), (3, 0, 2))
        self.assertEqual(second.processed, ())
        self.assertEqual(second.evidence, ())
        self.assertEqual([o.code for o in self.todo_skips(second)],
                         [Code.DUPLICATE.value])
        self.assertEqual(self.journal.ids(), intents_after_first)
        self.assertEqual(len(self.todos()), 1)       # 不多推一条待办

    # ---- 6：反向判据 —— 没点 / 不是执行人点的都不算完成 ----

    def test_an_open_stage_todo_waits_for_the_human_without_being_consumed(self):
        receipt = self.issue_stage()
        task_id = receipt.source.resource_id

        report = self.drive()

        self.assertEqual(self.live_loan().state, State.AWAITING_ISSUE)
        self.assertEqual(self.stock_counts(), (3, 2, 0))
        self.assertEqual(report.processed, ())
        self.assertEqual(report.evidence, ())
        self.assertEqual([o.code for o in self.todo_skips(report)], [Code.STATE.value])
        text = '\n'.join(format_drive_lines(report))
        self.assertIn(f'待人工 1 条：阶段待办未完成 1 条（{task_id}）', text)
        self.assertNotIn('  处理 ', text)

    def test_a_completion_by_anyone_but_the_executor_is_not_evidence(self):
        receipt = self.issue_stage()
        task_id = receipt.source.resource_id
        self.complete(task_id, actor=9000000999)

        report = self.drive()

        self.assertEqual(self.live_loan().state, State.AWAITING_ISSUE)
        self.assertEqual(self.stock_counts(), (3, 2, 0))
        self.assertEqual(report.processed, ())
        self.assertEqual([o.code for o in self.todo_skips(report)],
                         [Code.WRONG_PERSON.value])

    # ---- 7：标题是「发出去的那一份」，两状态都要查 ----

    def test_a_lost_create_receipt_is_claimed_by_the_sent_title_then_consumed(self):
        receipt = self.issue_stage_losing_the_reply()
        self.assertEqual(receipt.outcome.value, 'unknown')
        task_id, todo = next(iter(self.todos().items()))
        sent_title = todo['subject']
        self.assertIn(ITEM_NAME, sent_title)
        self.assertIn(BORROWER_NAME, sent_title)
        self.assertEqual(self.indexed(self.issue_operation)['title'], sent_title)
        self.assertTrue(self.indexed(self.issue_operation)['pending'])
        self.complete(task_id)
        # 显示名此刻已经查不到：现算的标题会退回 id，与发出去的那份**不同**。
        self.forget_display_names()

        report = self.drive()

        # 两状态都查 → 已完成的那条也被「认领」；认领后同一轮里就被消费掉。
        self.assertEqual(self.live_loan().state, State.BORROWED)
        self.assertEqual([e.source_id for e in report.evidence], [task_id])
        indexed = self.indexed(self.issue_operation)
        self.assertFalse(indexed.get('pending'))
        self.assertEqual(indexed['resource_id'], task_id)
        self.assertEqual(indexed['title'], sent_title)
        statuses = [call['status'] for call in load_state(self.state_path)['calls']
                    if call['verb'] == 'todo task list']
        self.assertEqual(statuses, ['false', 'true'])
        self.assertEqual(self.stock_counts(), (3, 0, 2))


if __name__ == '__main__':
    unittest.main()
