"""申请自动发现的物品解析：申请行按「工具」名称自己解析出物品记录（issue #89）。

#78 那版要求申请行携带「容器 + 记录 id」两格，而本机申请表只有 6 列、这两格是默认
空值，于是每一行都被判 ``CONFIG_RECONFIRM_REQUIRED`` —— 自动发现在真机上永不生效。
这里钉住 #89 的口径：

* 申请行**不再必须**携带两格：两格没给出时按「工具」单选题的**选项名**在库存容器里
  解析出唯一一条同名物品记录（比对逐字，两边只去首尾空白）；
* 两格都在、且这一行都填了时**优先**用它们（#78 老口径一字不改）；
* 只填了其中一格是半份答案：按 ``EVIDENCE_REQUIRED`` 记，既不猜另一半，也不绕开它
  退回按名称解析；
* 读不出来 / 名称对不上 / 名称对上不止一条 / 这一行没有可解析的名字：一律 fail
  closed，按行给出可见原因码，绝不自己造一条物品记录，也不取「第一条命中」；
* 解析只读：不写平台任何东西。

端到端（真人只填「工具/数量/归还日期」→ 一轮内出台账行 + 审批入口 + 审批待办）在
``test_application_intake`` 的 ``NameResolvedIntakeDriveTests`` 里。
"""
import importlib.util
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_memory_transport import MemoryTransport

from contracts.model import Code, ContractError, Resource, State
from contracts.ports import LedgerScope, RuntimeBinding
from integrations.dingtalk.adapter import DingTalkAdapter, TitleDisplay
from integrations.dingtalk.codec import _put_identity
from integrations.dingtalk.layout import (SYNTHETIC_APPLY_FIELDS,
                                          SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_FIELDS)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t89_apply_item_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t89_apply_item_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')

BORROWER, ITEM, NOW = _fixtures.BORROWER, _fixtures.ITEM, _fixtures.NOW
MANAGER = _fixtures.MANAGER

APPLY_CONTAINER = 'synthetic-apply-forms'
#: 库存（物品记录）所在容器：活绑定里就是台账作用域那一张表。
STOCK_CONTAINER = ITEM.container_id
#: 库存名称列的字段 ID（绑定里的 ``title_display.item_name_field``，非字段映射属性）。
NAME_FIELD = 'fldSYN-stock-item-name'
DISPLAY = TitleDisplay(item_name_field=NAME_FIELD)
#: 「工具」单选的答案：选项名 = 库存名称列里的物品名（合成值）。
TOOL = 'SYN-万用表'
_WRITE_COMMANDS = ('record.update', 'loan.create', 'form.create', 'todo.create')


class ApplicationItemResolutionTests(unittest.TestCase):
    """一行申请 → 一条物品记录：适配器侧的解析矩阵。"""

    def setUp(self):
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = SYNTHETIC_ENTRY_FIELDS
        self.apply_fields = SYNTHETIC_APPLY_FIELDS
        self.journal = _synthetic.SyntheticJournal()
        self.leases = _synthetic.SyntheticLease()
        self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.transport = MemoryTransport(
            self.fields, self.entry_fields, apply_container=APPLY_CONTAINER,
            apply_fields=self.apply_fields)
        self.adapter = self._adapter()

    def _adapter(self, **changes):
        kwargs = {'apply_fields': self.apply_fields,
                  'application_container': APPLY_CONTAINER,
                  'inventory_container': STOCK_CONTAINER,
                  'title_display': DISPLAY}
        kwargs.update(changes)
        return DingTalkAdapter(self.transport, self.journal, self.leases,
                               self.fields, self.entry_fields, **kwargs)

    def _seed_item(self, record_id='synthetic-item', name=TOOL, field=NAME_FIELD):
        ref = Resource('record', 'synthetic-org', STOCK_CONTAINER, record_id)
        self.transport.seed_record(ref, {field: name})
        return ref

    def _seed_apply_row(self, row_id='synthetic-apply-row-1', tool=TOOL, **extra):
        cells = {
            self.apply_fields.borrower: _put_identity(BORROWER),
            self.apply_fields.quantity: '2',
            self.apply_fields.occurred_at: (NOW - timedelta(days=1)).isoformat(),
            self.apply_fields.due_at: (NOW + timedelta(days=2)).isoformat(),
        }
        if tool is not None:
            cells[self.apply_fields.item] = tool
        cells.update(extra)
        row = Resource('form', 'synthetic-org', APPLY_CONTAINER, row_id)
        self.transport.seed_record(row, cells)
        return row

    def _refused(self, adapter=None, tool=TOOL, **extra):
        """读这一行必须失败，返回原因码；期间不许发生任何平台写入。"""
        self._seed_item()
        row = self._seed_apply_row(tool=tool, **extra)
        with self.assertRaises(ContractError) as raised:
            (adapter or self.adapter).read_application(row)
        self.assertEqual(
            [], [command for command, _ in self.transport.calls
                 if command in _WRITE_COMMANDS],
            '解析阶段不得写平台任何东西')
        return raised.exception.code

    def test_a_row_that_only_picks_a_tool_resolves_the_item_by_name(self):
        """本机申请表只有 6 列：一行只填「工具/数量/归还日期」也要解析出物品记录。"""
        item = self._seed_item()
        row = self._seed_apply_row()
        draft = self.adapter.read_application(row)
        self.assertEqual(draft.item, item)
        self.assertEqual(draft.quantity, 2)
        self.assertEqual(draft.borrower, BORROWER)
        self.assertFalse(draft.tracked)
        self.assertTrue(
            any(command == 'row.list' and args.get('container_id') == STOCK_CONTAINER
                for command, args in self.transport.calls),
            '按名称解析必须扫库存容器那一张表')
        self.assertEqual(
            [], [command for command, _ in self.transport.calls
                 if command in _WRITE_COMMANDS],
            '解析只读，不写平台')

    def test_surrounding_space_is_trimmed_on_both_sides(self):
        """比对前只去首尾空白：模板/输入法带出来的空格不算「另一个物品」。"""
        item = self._seed_item(name=f'  {TOOL}  ')
        row = self._seed_apply_row(tool=f' {TOOL} ')
        self.assertEqual(self.adapter.read_application(row).item, item)

    def test_a_row_that_carries_both_cells_wins_over_the_tool_name(self):
        """行内两格是 #78 的老口径，仍然优先：连库存表都不去扫。"""
        named = self._seed_item('synthetic-item-1', TOOL)
        other = self._seed_item('synthetic-item-2', 'SYN-绝缘手套')
        row = self._seed_apply_row(
            tool=TOOL,
            **{self.apply_fields.item_container: other.container_id,
               self.apply_fields.item_id: other.resource_id})
        draft = self.adapter.read_application(row)
        self.assertEqual(draft.item, other)
        self.assertNotEqual(draft.item, named)
        self.assertNotIn('row.list', [command for command, _ in self.transport.calls])

    def test_a_half_answered_pair_fails_closed_without_falling_back_to_the_name(self):
        """只填了一格：半份答案不猜另一半，也不绕开它去按名字找。"""
        halves = ({self.apply_fields.item_container: STOCK_CONTAINER},
                  {self.apply_fields.item_id: 'synthetic-item'})
        for index, extra in enumerate(halves):
            with self.subTest(half=sorted(extra)):
                self.transport.calls.clear()
                self._seed_item()
                row = self._seed_apply_row(row_id=f'synthetic-apply-row-{index}',
                                           **extra)
                with self.assertRaises(ContractError) as raised:
                    self.adapter.read_application(row)
                self.assertEqual(raised.exception.code, Code.EVIDENCE)
                self.assertNotIn('row.list',
                                 [command for command, _ in self.transport.calls],
                                 '半份答案不得退回按名称解析')

    def test_a_name_that_matches_no_record_fails_closed(self):
        self.assertEqual(self._refused(tool='SYN-绝缘手套'), Code.EVIDENCE)

    def test_two_records_with_the_same_name_fail_closed(self):
        """多命中不取第一条：同名两条记录里选一条就是猜。"""
        self._seed_item('synthetic-item-1', TOOL)
        self._seed_item('synthetic-item-2', TOOL)
        row = self._seed_apply_row()
        with self.assertRaises(ContractError) as raised:
            self.adapter.read_application(row)
        self.assertEqual(raised.exception.code, Code.EVIDENCE)

    def test_a_name_that_only_differs_by_a_prefix_is_not_a_match(self):
        """逐字比对：不做前缀 / 包含 / 大小写模糊匹配（「万用表」≠「SYN-万用表」）。

        真机现存数据就是这种形状：申请表的「工具」选项名与库存名称列差了前缀。
        模糊匹配会建出一条指向**另一个工具**的台账行，所以宁可逐行 fail closed。
        """
        self.assertEqual(self._refused(tool='万用表'), Code.EVIDENCE)

    def test_a_record_without_a_name_cannot_be_the_named_one(self):
        """没有名称格的记录不可能是「名字叫 X」的那一条：跳过它不算丢掉候选。"""
        item = self._seed_item('synthetic-item-1')
        self.transport.seed_record(
            Resource('record', 'synthetic-org', STOCK_CONTAINER, 'synthetic-item-2'),
            {self.fields.available: '1'})
        row = self._seed_apply_row()
        self.assertEqual(self.adapter.read_application(row).item, item)

    def test_a_name_cell_that_is_not_a_name_fails_closed(self):
        """名称格出现却不是名字（未观察形态）：不静默跳掉，否则多命中看不出来。"""
        self._seed_item('synthetic-item-1')
        self._seed_item('synthetic-item-2', name=7)
        row = self._seed_apply_row()
        with self.assertRaises(ContractError) as raised:
            self.adapter.read_application(row)
        self.assertEqual(raised.exception.code, Code.EVIDENCE)

    def test_a_row_without_a_tool_answer_fails_closed(self):
        """这一行没填「工具」：没有可解析的名字，不猜是哪个物品。"""
        self.assertEqual(self._refused(tool=None), Code.EVIDENCE)

    def test_an_instance_that_declares_no_item_question_reports_config(self):
        """两格与「工具」都没声明：还是 #78 的逐行 CONFIG（不静默建行）。"""
        fields = replace(self.apply_fields, item='unset:item',
                         item_container='unset:item_container',
                         item_id='unset:item_id')
        self.assertEqual(self._refused(adapter=self._adapter(apply_fields=fields)),
                         Code.CONFIG)

    def test_the_name_column_has_to_be_declared(self):
        """没有 title_display.item_name_field 就没有名字可读：CONFIG，不是猜。"""
        self.assertEqual(self._refused(adapter=self._adapter(title_display=None)),
                         Code.CONFIG)

    def test_the_inventory_container_has_to_be_given(self):
        """没有库存容器就没有比对范围：CONFIG。"""
        self.assertEqual(self._refused(adapter=self._adapter(inventory_container=None)),
                         Code.CONFIG)

    def test_a_denied_inventory_read_is_not_an_empty_inventory(self):
        """库存表读不到（无权限）：按可见原因码跳过，绝不读成「表里没有这个物品」。

        ``record query --all`` 的真机回执里没有 ``status`` 字段（成功与失败都只有
        ``hasMore/records``），所以一次失败在适配器里落在形态这一层：可见原因码是
        ``EVIDENCE_REQUIRED``，不是「0 条同名记录」。与申请表扫描（#78）同一口径。
        """
        self._seed_item()
        row = self._seed_apply_row()
        self.transport.fail_codes['row.list'] = 'FORBIDDEN'
        with self.assertRaises(ContractError) as raised:
            self.adapter.read_application(row)
        self.assertEqual(raised.exception.code, Code.EVIDENCE)

    def test_an_empty_inventory_table_fails_closed(self):
        """库存表真的空（records: null + hasMore: false）：没有可解析的记录，跳过。"""
        row = self._seed_apply_row()
        with self.assertRaises(ContractError) as raised:
            self.adapter.read_application(row)
        self.assertEqual(raised.exception.code, Code.EVIDENCE)

    def test_a_dropped_inventory_reply_fails_closed(self):
        """回执丢了（None）：fail closed，不按「没查到」继续。"""
        self._seed_item()
        row = self._seed_apply_row()
        self.transport.drop_once.append('row.list')
        with self.assertRaises(ContractError) as raised:
            self.adapter.read_application(row)
        self.assertEqual(raised.exception.code, Code.EVIDENCE)

    def test_the_resolved_item_lands_inside_the_binding_scope(self):
        """解析出来的物品必须落在绑定作用域里（否则建行时 check_binding 会拦下）。"""
        item = self._seed_item()
        row = self._seed_apply_row()
        draft = self.adapter.read_application(row)
        self.assertEqual(LedgerScope.from_record(draft.item),
                         LedgerScope.from_record(item))
        self.assertEqual(draft.item.resource_id, item.resource_id)


class NameResolvedDraftTests(unittest.TestCase):
    """解析出来的物品要能直接进台账行：draft → 建行 → 回读，链路键与绑定都不变。"""

    def test_a_name_resolved_draft_creates_the_ledger_row_for_that_record(self):
        transport = MemoryTransport(SYNTHETIC_FIELDS, SYNTHETIC_ENTRY_FIELDS,
                                    apply_container=APPLY_CONTAINER,
                                    apply_fields=SYNTHETIC_APPLY_FIELDS)
        journal = _synthetic.SyntheticJournal()
        leases = _synthetic.SyntheticLease()
        item = Resource('record', 'synthetic-org', STOCK_CONTAINER, 'synthetic-item')
        transport.seed_record(item, {NAME_FIELD: TOOL})
        adapter = DingTalkAdapter(
            transport, journal, leases, SYNTHETIC_FIELDS, SYNTHETIC_ENTRY_FIELDS,
            apply_fields=SYNTHETIC_APPLY_FIELDS, application_container=APPLY_CONTAINER,
            inventory_container=STOCK_CONTAINER, title_display=DISPLAY,
            loan_container='synthetic-loans')
        row = Resource('form', 'synthetic-org', APPLY_CONTAINER, 'synthetic-apply-row-1')
        transport.seed_record(row, {
            SYNTHETIC_APPLY_FIELDS.borrower: _put_identity(BORROWER),
            SYNTHETIC_APPLY_FIELDS.quantity: '2',
            SYNTHETIC_APPLY_FIELDS.occurred_at: (NOW - timedelta(days=1)).isoformat(),
            SYNTHETIC_APPLY_FIELDS.due_at: (NOW + timedelta(days=2)).isoformat(),
            SYNTHETIC_APPLY_FIELDS.item: TOOL,
        })
        binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(item), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        lease = leases.acquire(LedgerScope.from_record(item), MANAGER)
        draft = adapter.read_application(row)
        ref = adapter.create_application_loan(draft, binding, lease)
        written = adapter.read_loan(ref)
        self.assertEqual(written.item, item)
        self.assertEqual(written.quantity, 2)
        self.assertEqual(written.due_at, draft.due_at)
        self.assertEqual(written.state, State.AWAITING_APPROVAL)
        self.assertEqual(written.application_evidence, draft.marker)
