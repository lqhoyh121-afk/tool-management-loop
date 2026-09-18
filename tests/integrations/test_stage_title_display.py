"""阶段待办标题里的「谁 / 什么 / 什么时候」与审批入口链接（issue #76）。

标题是执行人在待办列表里唯一的信息来源，同时也是超时回查的匹配键（transport 把发出去
的那份记在阶段索引里）。这里断三件事：

1. 领用确认 / 归还确认的标题能看出 借用人 + 物品 + 到期；
2. 显示名查不到（没配、读空、报错、没回包）时退回资源 id / userId，**阶段照样建得出来**；
3. 审批待办只在绑定给了链接时才带链接，没给就保持老标题 —— 链接只负责把人送到填写处，
   审批结论的真源仍是入口行的「决定」列，没有被这条待办改动。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t031_fake_dws import load_state, save_state
from t03_layout import entry_fields_from
from t03_live_cells import declared_kinds
from t03_memory_transport import MemoryTransport

from contracts.model import Action, Identity, Outcome, Resource, State
from contracts.ports import (LedgerScope, RuntimeBinding, StageRequest,
                             stage_operation_id)
from integrations.dingtalk.adapter import (DingTalkAdapter, DisplayNames,
                                           TitleDisplay, stage_title)
from integrations.dingtalk.codec import encode_inventory, encode_loan
from integrations.dingtalk.dws_transport import DwsTransport, contact_name
from integrations.dingtalk.layout import SYNTHETIC_FIELDS


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t76_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t76_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
loan, stock = _fixtures.loan, _fixtures.stock
MANAGER = _fixtures.MANAGER
BORROWER = _fixtures.BORROWER
SyntheticJournal = _synthetic.SyntheticJournal
SyntheticLease = _synthetic.SyntheticLease

APPROVER = Identity('contact', 'synthetic-org', 'synthetic-approver')
ITEM_NAME_FIELD = 'fldSYN-item-name'
ITEM_NAME = 'SYN-万用表'
BORROWER_NAME = 'SYN-张三'
ENTRY_URL = 'https://example.invalid/synthetic-approve-form'
FORM_CONTAINER = 'baseForm/tblForm'
TODO_CONTAINER = 'todoSpace/executors'
FAKE_DWS = Path(__file__).resolve().parent / 't031_fake_dws.py'


def shanghai_due(current):
    return current.due_at.astimezone(
        timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')


ACTION_STATES = {Action.APPROVE: State.AWAITING_APPROVAL,
                 Action.ISSUE: State.AWAITING_ISSUE,
                 Action.RETURN: State.AWAITING_RETURN}


def actor_for(current, action):
    """阶段入口行只由该阶段点名的角色建：审批人 / 借用人 / 管理人。"""
    if action is Action.APPROVE:
        return current.approver
    if action is Action.REQUEST_RETURN:
        return current.borrower
    return current.manager


class StageTitleTests(unittest.TestCase):
    """纯函数：标题里该出现谁、出现什么、什么时候出现链接。"""

    def setUp(self):
        self.current = loan()

    def test_issue_title_shows_borrower_item_and_due(self):
        title = stage_title(self.current, Action.ISSUE,
                            DisplayNames(ITEM_NAME, BORROWER_NAME))
        self.assertIn('待领用确认', title)
        self.assertIn(ITEM_NAME, title)
        self.assertIn(BORROWER_NAME, title)
        self.assertIn(shanghai_due(self.current), title)
        self.assertIn(f'×{self.current.quantity}', title)
        self.assertIn(self.current.ref.resource_id, title)

    def test_return_title_shows_borrower_item_and_due(self):
        title = stage_title(self.current, Action.RETURN,
                            DisplayNames(ITEM_NAME, BORROWER_NAME))
        self.assertIn('待归还确认', title)
        self.assertIn(ITEM_NAME, title)
        self.assertIn(BORROWER_NAME, title)
        self.assertIn(shanghai_due(self.current), title)

    def test_missing_names_fall_back_to_ids(self):
        title = stage_title(self.current, Action.ISSUE)
        self.assertIn(self.current.item.resource_id, title)
        self.assertIn(self.current.borrower.user_id, title)
        self.assertIn(shanghai_due(self.current), title)

    def test_blank_names_fall_back_to_ids(self):
        title = stage_title(self.current, Action.ISSUE, DisplayNames('   ', ''))
        self.assertIn(self.current.item.resource_id, title)
        self.assertIn(self.current.borrower.user_id, title)

    def test_approve_title_carries_the_entry_link(self):
        title = stage_title(self.current, Action.APPROVE,
                            DisplayNames(ITEM_NAME, BORROWER_NAME), ENTRY_URL)
        self.assertIn(f'填表→ {ENTRY_URL}', title)
        self.assertIn(ITEM_NAME, title)
        self.assertIn(BORROWER_NAME, title)

    def test_approve_title_without_a_link_keeps_the_old_wording(self):
        title = stage_title(self.current, Action.APPROVE)
        self.assertNotIn('填表→', title)
        self.assertIn('填：决定 + 发生时间', title)

    def test_blank_link_is_not_appended(self):
        title = stage_title(self.current, Action.APPROVE, None, '   ')
        self.assertNotIn('填表→', title)

    def test_unobserved_action_stays_a_bare_business_number(self):
        title = stage_title(self.current, Action.CANCEL,
                            DisplayNames(ITEM_NAME, BORROWER_NAME), ENTRY_URL)
        self.assertEqual(title, f'{Action.CANCEL.value} ｜ 单号 '
                                f'{self.current.ref.resource_id}')


class AdapterTitleTests(unittest.TestCase):
    """适配器层：查名的时机与失败兜底，用内存传输层（不碰真机）。"""

    def setUp(self):
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = entry_fields_from(self.fields)
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(_fixtures.ITEM), MANAGER)
        self.current = replace(loan(), approver=APPROVER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(_fixtures.ITEM), 'synthetic-config-v1',
            APPROVER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = MemoryTransport(self.fields, self.entry_fields)

    def adapter(self, title_display=None):
        return DingTalkAdapter(self.transport, self.journal, self.leases,
                               self.fields, self.entry_fields,
                               title_display=title_display)

    def full_display(self):
        return TitleDisplay(ITEM_NAME_FIELD, True, ENTRY_URL)

    def stage(self, action, adapter):
        current = replace(self.current, state=ACTION_STATES[action])
        self.transport.seed_record(current.ref, encode_loan(current, self.fields))
        self.transport.seed_record(stock().ref,
                                   encode_inventory(stock(), self.fields))
        request = StageRequest(stage_operation_id(current, action),
                               current, action, actor_for(current, action))
        return adapter.create_stage(request, self.binding, self.lease)

    def todo_titles(self):
        return [arguments['title']
                for _command, arguments in self.transport.writes_of('todo.create')]

    def name_lookups(self):
        return [arguments for command, arguments in self.transport.calls
                if command == 'title.names']

    def test_issue_title_shows_names_and_asks_for_them_once(self):
        self.transport.title_names = {'item_name': ITEM_NAME,
                                      'borrower_name': BORROWER_NAME}
        receipt = self.stage(Action.ISSUE, self.adapter(self.full_display()))
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        title = self.todo_titles()[0]
        self.assertIn(ITEM_NAME, title)
        self.assertIn(BORROWER_NAME, title)
        self.assertIn(shanghai_due(self.current), title)
        self.assertNotIn(self.current.item.resource_id, title)
        lookups = self.name_lookups()
        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0]['item_name_field'], ITEM_NAME_FIELD)
        self.assertEqual(lookups[0]['item_id'], self.current.item.resource_id)
        self.assertEqual(lookups[0]['borrower'], self.current.borrower.user_id)
        self.assertIs(lookups[0]['borrower_names'], True)

    def test_unknown_names_fall_back_to_ids_without_blocking_the_stage(self):
        self.transport.title_names = {}
        receipt = self.stage(Action.ISSUE, self.adapter(self.full_display()))
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        title = self.todo_titles()[0]
        self.assertIn(self.current.item.resource_id, title)
        self.assertIn(self.current.borrower.user_id, title)

    def test_failed_name_lookup_falls_back_to_ids(self):
        self.transport.title_names_code = 'PERMISSION_DENIED'
        receipt = self.stage(Action.ISSUE, self.adapter(self.full_display()))
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        title = self.todo_titles()[0]
        self.assertIn(self.current.item.resource_id, title)
        self.assertIn(self.current.borrower.user_id, title)
        self.assertIn(shanghai_due(self.current), title)

    def test_name_lookup_without_a_reply_falls_back_to_ids(self):
        self.transport.drop_once = ['title.names']
        receipt = self.stage(Action.ISSUE, self.adapter(self.full_display()))
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertIn(self.current.item.resource_id, self.todo_titles()[0])

    def test_no_lookup_when_the_binding_did_not_ask_for_names(self):
        receipt = self.stage(Action.ISSUE, self.adapter())
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertEqual(self.name_lookups(), [])
        self.assertIn(self.current.item.resource_id, self.todo_titles()[0])

    def test_approve_nudge_carries_the_entry_link(self):
        receipt = self.stage(Action.APPROVE, self.adapter(self.full_display()))
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertEqual(receipt.source.kind, 'form')  # 真源仍是入口行
        title = self.todo_titles()[0]
        self.assertIn(f'填表→ {ENTRY_URL}', title)
        recipients = [arguments['actor']
                      for _command, arguments in self.transport.writes_of('todo.create')]
        self.assertEqual(recipients, [APPROVER.user_id])

    def test_link_only_config_does_not_trigger_a_name_lookup(self):
        display = TitleDisplay(approve_entry_url=ENTRY_URL)
        receipt = self.stage(Action.APPROVE, self.adapter(display))
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertEqual(self.name_lookups(), [])
        self.assertIn(f'填表→ {ENTRY_URL}', self.todo_titles()[0])

    def test_approve_nudge_without_a_link_keeps_the_old_title(self):
        receipt = self.stage(Action.APPROVE, self.adapter())
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertNotIn('填表→', self.todo_titles()[0])


class DwsTitleTests(unittest.TestCase):
    """真 dws 传输层 + 子进程替身：标题落到 todo 里与真机形态一致。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.state_path = self.work / 'fake-state.json'
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = entry_fields_from(self.fields)
        state = load_state(self.state_path)
        state['kinds'] = declared_kinds(self.fields, self.entry_fields)
        save_state(self.state_path, state)
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        # 容器必须是真机那种 ``baseId/tableId`` 形态，否则读路径会 fail closed。
        self.item = Resource('record', 'synthetic-org', 'baseStock/tblStock', 'recItem')
        self.current = replace(loan(), item=self.item, approver=APPROVER)
        self.lease = self.leases.acquire(LedgerScope.from_record(self.item), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(self.item), 'synthetic-config-v1',
            APPROVER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = DwsTransport(
            [sys.executable, str(FAKE_DWS)], self.fields,
            form_container=FORM_CONTAINER, todo_container=TODO_CONTAINER,
            work_dir=self.work / 'runtime', entry_fields=self.entry_fields,
            extra_env={'FAKE_DWS_STATE': str(self.state_path)},
        )
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            title_display=TitleDisplay(ITEM_NAME_FIELD, True, ENTRY_URL))
        self.item_name = ITEM_NAME
        self.borrower_name = BORROWER_NAME

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self, current):
        """把台账行、库存行和通讯录写成真机读得到的样子（只改这几处，别的键保留）。"""
        state = load_state(self.state_path)
        state['records'][f'{current.ref.container_id}/{current.ref.resource_id}'] = (
            encode_loan(current, self.fields))
        cells = encode_inventory(replace(stock(), ref=self.item), self.fields)
        if self.item_name:
            cells[ITEM_NAME_FIELD] = self.item_name
        state['records'][f'{self.item.container_id}/{self.item.resource_id}'] = cells
        state['contact_names'] = ({current.borrower.user_id: self.borrower_name}
                                  if self.borrower_name else {})
        save_state(self.state_path, state)

    def stage(self, action):
        current = replace(self.current, state=ACTION_STATES[action])
        self.write_state(current)
        request = StageRequest(stage_operation_id(current, action),
                               current, action, actor_for(current, action))
        return self.adapter.create_stage(request, self.binding, self.lease)

    def todo_titles(self):
        return [todo['subject'] for todo in load_state(self.state_path)['todos'].values()]

    def test_issue_title_lands_with_names_and_due(self):
        receipt = self.stage(Action.ISSUE)
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        titles = self.todo_titles()
        self.assertEqual(len(titles), 1)
        self.assertIn(ITEM_NAME, titles[0])
        self.assertIn(BORROWER_NAME, titles[0])
        self.assertIn(shanghai_due(self.current), titles[0])
        self.assertIn(self.current.ref.resource_id, titles[0])

    def test_approve_nudge_lands_with_the_entry_link(self):
        receipt = self.stage(Action.APPROVE)
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        titles = self.todo_titles()
        self.assertEqual(len(titles), 1)
        self.assertIn(f'填表→ {ENTRY_URL}', titles[0])
        self.assertIn(ITEM_NAME, titles[0])

    def test_unknown_item_name_and_borrower_fall_back_to_ids(self):
        self.item_name = None       # 库存行里没有物品名称列
        self.borrower_name = None   # 通讯录里也查不到这个人
        receipt = self.stage(Action.ISSUE)
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        title = self.todo_titles()[0]
        self.assertIn(self.item.resource_id, title)
        self.assertIn(self.current.borrower.user_id, title)

    def test_blocked_contact_lookup_falls_back_to_the_user_id(self):
        state = load_state(self.state_path)
        state['fail'] = {'contact user get': 'PERMISSION_DENIED'}
        save_state(self.state_path, state)
        receipt = self.stage(Action.ISSUE)
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        title = self.todo_titles()[0]
        self.assertIn(ITEM_NAME, title)  # 物品名读得到
        self.assertIn(self.current.borrower.user_id, title)


class ContactNameTests(unittest.TestCase):
    """通讯录报文取值：只认真机观测到的位置，其余当没查到。"""

    def test_observed_shape(self):
        payload = json.loads(json.dumps({
            'result': [{'orgEmployeeModel': {'orgUserName': BORROWER_NAME}}],
            'success': True,
        }))
        self.assertEqual(contact_name(payload), BORROWER_NAME)

    def test_other_shapes_are_a_miss(self):
        for payload in (None, {}, {'result': []}, {'result': {}},
                        {'result': [{'orgEmployeeModel': {}}]},
                        {'result': 'synthetic-not-a-list'}):
            self.assertEqual(contact_name(payload), '')
        self.assertEqual(contact_name({'result': [{'userName': BORROWER_NAME}]}),
                         BORROWER_NAME)


if __name__ == '__main__':
    unittest.main()
