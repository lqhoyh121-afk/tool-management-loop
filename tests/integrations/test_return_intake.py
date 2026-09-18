"""归还表单行的自动发现：适配器读行 + 一轮驱动推进到待归还确认（issue #87）。

这一层回答三个只有真读/写路径才能回答的问题：

* 归还表单行与本机建的阶段入口行**共用一张结果表**，扫描与逐行读取是否分得开
  （入口行带台账单据指向，归还提交只有「借用人 + 归还时间」）；
* 一轮驱动里真人填完的归还行是否真的**自己**走到 ``awaiting_return_confirmation`` 并推出
  【待归还确认】待办，且工作队列里只多一条 ``kind='event'`` 引用（零人工登记）；
* 失败面是否 fail closed：表读不出来（``records: null`` 而 ``hasMore`` 不为 false）、
  行读不出来、必填缺、定位不到唯一一张借出中的单 —— 都以原因码出现在报告里，
  且一个队列条目都不写。
"""
import importlib.util
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_memory_transport import MemoryTransport

from bootstrap.drive import DriveLoop, FileSources, format_drive_lines
from bootstrap.inbox import REGISTER_NAME, ApplicationIntake
from bootstrap.instance import MachineLock
from bootstrap.journal import FileJournal
from bootstrap.returns import ReturnIntake, format_return_lines
from contracts.model import Action, Code, ContractError, Resource, State
from contracts.ports import LedgerScope, RuntimeBinding, stage_operation_id
from integrations.dingtalk.adapter import DingTalkAdapter
from integrations.dingtalk.codec import _put_identity, encode_inventory, encode_loan
from integrations.dingtalk.layout import (SYNTHETIC_APPLY_FIELDS, SYNTHETIC_ENTRY_FIELDS,
                                          SYNTHETIC_FIELDS, SYNTHETIC_RETURN_FORM_FIELDS,
                                          ReturnFormFieldMap)
from workflow.engine import LendingEngine


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
fixtures = _load('t87_return_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
synthetic = _load('t87_return_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')

BORROWER, ITEM, NOW, MANAGER = (fixtures.BORROWER, fixtures.ITEM, fixtures.NOW,
                                fixtures.MANAGER)
ENTRY_CONTAINER = 'synthetic-forms'
LOAN_CONTAINER = 'synthetic-loans'
APPLICATION_CONTAINER = 'synthetic-apply-forms'
RETURN_ROW = 'synthetic-return-row'
#: 真人刚提交的归还时间（水位启用之后）。
RETURNED_AT = NOW - timedelta(hours=1)
#: 启用水位（``return_intake.since``）：早于上面那笔归还。
WATERMARK = NOW - timedelta(days=3)
#: 水位之前的历史归还：启用当天表里往往已经有这种行。
HISTORIC = NOW - timedelta(days=5)
ITEM2 = Resource('record', 'synthetic-org', 'synthetic-stock', 'synthetic-item-2')


def _live_select(name):
    """singleSelect 的真机回读形态：``{id, name}``（替身按字段类型物化）。"""
    return {'id': f'SYNTHETIC-rand-{name}', 'name': name}


def borrowed_loan(resource_id=None, item=None, state=State.BORROWED):
    """一张借出中的单：写载荷 + 真机回读形态（``state`` 是单选，``tracked`` 是布尔）。"""
    current = replace(fixtures.loan(), state=state)
    if resource_id is not None or item is not None:
        current = replace(
            current,
            ref=replace(current.ref, resource_id=resource_id or current.ref.resource_id),
            item=item or current.item)
    cells = encode_loan(current, SYNTHETIC_FIELDS)
    cells[SYNTHETIC_FIELDS.state] = _live_select(current.state.value)
    cells[SYNTHETIC_FIELDS.tracked] = _live_select('false')
    return current, cells


def return_row_cells(occurred=RETURNED_AT, borrower=BORROWER, item=None):
    """真人填的归还行：借用人 + 归还时间（必填），外加可选的「归还物品」格。"""
    cells = {
        SYNTHETIC_RETURN_FORM_FIELDS.borrower: _put_identity(borrower),
        SYNTHETIC_RETURN_FORM_FIELDS.occurred_at: occurred.isoformat(),
    }
    if item is not None:
        cells[SYNTHETIC_RETURN_FORM_FIELDS.item] = item
    return cells


def entry_row_cells(loan_ref, borrower=BORROWER):
    """本机为阶段建的入口行：带台账单据指向（这正是它与归还提交的分界）。"""
    return {
        SYNTHETIC_ENTRY_FIELDS.loan_container: loan_ref.container_id,
        SYNTHETIC_ENTRY_FIELDS.loan_id: loan_ref.resource_id,
        SYNTHETIC_ENTRY_FIELDS.borrower: _put_identity(borrower),
    }


def _adapter(store, locks, **kwargs):
    transport = kwargs.pop('transport')
    fields = {'return_form_fields': SYNTHETIC_RETURN_FORM_FIELDS}
    fields.update(kwargs)
    return DingTalkAdapter(
        transport, store, locks, SYNTHETIC_FIELDS, SYNTHETIC_ENTRY_FIELDS,
        entry_container=ENTRY_CONTAINER, loan_container=LOAN_CONTAINER, **fields)


class ShapeChangingTransport(MemoryTransport):
    """内存替身 + 能把扫表报文换成「读不到」的形态（真机 ``records: null``）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.list_payload = None

    def exchange(self, command, arguments):
        if command == 'row.list' and self.list_payload is not None:
            self.calls.append((command, dict(arguments)))
            return self.list_payload
        return super().exchange(command, arguments)


class ReturnRowReadTests(unittest.TestCase):
    """适配器侧：扫表 / 判是不是归还提交 / 读成草稿。"""

    def setUp(self):
        self.journal = synthetic.SyntheticJournal()
        self.leases = synthetic.SyntheticLease()
        self.transport = MemoryTransport(SYNTHETIC_FIELDS, SYNTHETIC_ENTRY_FIELDS)
        self.adapter = _adapter(self.journal, self.leases, transport=self.transport)

    def seed(self, row_id, cells):
        ref = Resource('form', 'synthetic-org', ENTRY_CONTAINER, row_id)
        self.transport.seed_record(ref, cells)
        return ref

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_scan_lists_every_row_of_the_return_table_including_entry_rows(self):
        """扫描只回答「有哪些行」：入口行也在里面，过滤会把读不到当成没有归还。

        扫描行同时带上**这一次回读的单元格**（发现侧据此就地判掉入口行 / 水位之前的行，
        不再为每一行发一次平台读）：这两件事一起钉住。
        """
        self.assertEqual(self.adapter.pending_returns('synthetic-org'), ())
        loan, _cells = borrowed_loan()
        row = self.seed(RETURN_ROW, return_row_cells())
        entry = self.seed('synthetic-entry-row', entry_row_cells(loan.ref))
        scanned = self.adapter.pending_returns('synthetic-org')
        self.assertEqual({item.ref for item in scanned}, {entry, row})
        by_id = {item.resource_id: item for item in scanned}
        before = len(self.transport.writes_of('record.query'))
        # 归还行：扫描内容就够判成草稿（不再回读一次平台）。
        self.assertEqual(self.adapter.read_return(by_id[RETURN_ROW]).occurred_at, RETURNED_AT)
        # 入口行：扫描内容就够判成「不是归还提交」。
        self.assertIsNone(self.adapter.read_return(by_id['synthetic-entry-row']))
        self.assertEqual(self.transport.writes_of('record.query')[before:], [])

    def test_a_submitted_return_row_reads_into_a_draft(self):
        row = self.seed(RETURN_ROW, return_row_cells())
        draft = self.adapter.read_return(row)
        self.assertEqual(draft.source, row)
        self.assertEqual(draft.borrower, BORROWER)
        self.assertEqual(draft.occurred_at, RETURNED_AT)

    def test_a_stage_entry_row_is_not_a_return_submission(self):
        """入口行也带「借用人」（两边是同一列），单据指向才是分界。

        尤其是本机为「待归还请求」建的入口行：借用人已填、归还时间还没人填 ——
        先判归还答案就会把它每轮都报成「必填缺」。
        """
        loan, _cells = borrowed_loan()
        row = self.seed('synthetic-request-return-entry', entry_row_cells(loan.ref))
        self.assertIsNone(self.adapter.read_return(row))

    def test_a_row_that_was_never_filled_is_not_a_return_submission(self):
        row = self.seed('synthetic-blank-row', {})
        self.assertIsNone(self.adapter.read_return(row))

    def test_a_borrower_without_a_return_time_fails_closed(self):
        """只填了借用人：必填缺 → 可见的失败，绝不静默当成「不是归还提交」。"""
        cells = {SYNTHETIC_RETURN_FORM_FIELDS.borrower: _put_identity(BORROWER)}
        row = self.seed(RETURN_ROW, cells)
        self.blocked(Code.EVIDENCE, lambda: self.adapter.read_return(row))

    def test_a_row_that_cannot_be_read_fails_closed(self):
        """扫到了行、但这一行读不回来：fail closed，不当成「不是归还提交」。"""
        row = self.seed(RETURN_ROW, return_row_cells())
        del self.transport.records[('synthetic-org', ENTRY_CONTAINER, row.resource_id)]
        self.blocked(Code.EVIDENCE, lambda: self.adapter.read_return(row))

    def test_a_row_whose_read_never_replies_fails_closed(self):
        """读行没有回包：``WRITE_UNKNOWN_QUERY_FIRST``（同样不许猜）。"""
        row = self.seed(RETURN_ROW, return_row_cells())
        self.transport.drop_commands = {'record.query'}
        self.blocked(Code.UNKNOWN, lambda: self.adapter.read_return(row))

    def test_a_row_from_another_container_fails_closed(self):
        other = Resource('form', 'synthetic-org', 'synthetic-other-table', RETURN_ROW)
        self.transport.seed_record(other, return_row_cells())
        self.blocked(Code.EVIDENCE, lambda: self.adapter.read_return(other))

    def test_a_binding_without_the_two_required_cells_fails_closed(self):
        """归还两格没声明：不是「没有新归还」而是 CONFIG（否则每行都读成「不是提交」）。"""
        adapter = _adapter(
            self.journal, self.leases, transport=self.transport,
            return_form_fields=ReturnFormFieldMap(borrower='',
                                                  occurred_at='unset:return_time',
                                                  item=''))
        self.blocked(Code.CONFIG, lambda: adapter.pending_returns('synthetic-org'))


class ReturnIntakeDriveTests(unittest.TestCase):
    """验收主路径：真人填完一笔归还 → 零人工登记 → 一轮内出现待归还确认待办。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(parents=True)
        self.locks = MachineLock(self.root / 'locks')
        self.store = FileJournal(self.runtime / 'operations')
        self.transport = ShapeChangingTransport(SYNTHETIC_FIELDS, SYNTHETIC_ENTRY_FIELDS,
                                                apply_fields=SYNTHETIC_APPLY_FIELDS)
        self.adapter = _adapter(self.store, self.locks, transport=self.transport,
                                apply_fields=SYNTHETIC_APPLY_FIELDS,
                                application_container=APPLICATION_CONTAINER)
        self.sources = FileSources(self.runtime / 'sources.json')

    def tearDown(self):
        self.temp.cleanup()

    def lend(self, resource_id=None, item=None, state=State.BORROWED):
        current, cells = borrowed_loan(resource_id, item, state)
        self.transport.seed_record(current.ref, cells)
        self.transport.seed_record(current.item,
                                   encode_inventory(fixtures.stock(), SYNTHETIC_FIELDS))
        return current

    def submit(self, row_id=RETURN_ROW, occurred=RETURNED_AT, item=None, borrower=BORROWER):
        ref = Resource('form', 'synthetic-org', ENTRY_CONTAINER, row_id)
        self.transport.seed_record(ref, return_row_cells(occurred, borrower, item))
        return ref

    def start(self, since=WATERMARK, applications=False):
        binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        engine = LendingEngine(self.adapter, self.adapter, self.adapter, self.store,
                               self.locks, binding)
        engine.start(binding.ledger, binding.account)
        intake = (ApplicationIntake(engine, self.adapter, self.sources, self.store,
                                    self.locks, self.runtime / REGISTER_NAME, since=None)
                  if applications else None)
        returns = ReturnIntake(engine, self.adapter, self.sources, self.store, self.locks,
                               since=since)
        return engine, DriveLoop(engine, self.sources, self.store, self.locks,
                                 intake=intake, returns=returns)

    def pass_once(self, since=WATERMARK, applications=False):
        engine, loop = self.start(since=since, applications=applications)
        try:
            return loop.run()
        finally:
            engine.stop()

    def stage_writes(self):
        return (len(self.transport.writes_of('form.create')),
                len(self.transport.writes_of('todo.create')))

    def test_one_pass_turns_a_submitted_row_into_a_return_confirmation_todo(self):
        loan = self.lend()
        row = self.submit()
        report = self.pass_once()
        # 发现账目：扫到 1 行，本机一个引用都不认识，这轮登记 1 条，没有跳过。
        self.assertEqual(report.returns.scanned, 1)
        self.assertEqual(report.returns.known, 0)
        self.assertEqual(report.returns.skipped, ())
        self.assertEqual([finding.source for finding in report.returns.findings], [row])
        # 队列：只多一条 kind=event 引用，来源就是这一行、单据就是定位到的那张单。
        queue = self.sources.pending()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].kind, 'event')
        self.assertEqual(queue[0].source, row)
        self.assertEqual(queue[0].loan_ref, loan.ref)
        # 一轮内真的推到了待归还确认，并建出【待归还确认】入口。
        self.assertEqual(self.adapter.read_loan(loan.ref).state, State.AWAITING_RETURN)
        self.assertEqual(self.transport.stages[
            stage_operation_id(loan, Action.RETURN)]['kind'], 'todo')
        self.assertEqual(len(report.processed), 1)
        self.assertEqual(report.processed[0].kind, 'event')

    def test_a_second_pass_and_a_restart_add_nothing(self):
        self.lend()
        self.submit()
        self.pass_once()
        first = (self.stage_writes(), len(self.sources.pending()))
        report = self.pass_once()
        # 扫到 2 行：归还行与上一轮为「待归还请求」建的那张入口行，两行本机都已有引用。
        self.assertEqual(report.returns.known, report.returns.scanned)
        self.assertEqual(report.returns.scanned, 2)
        self.assertEqual(report.returns.findings, ())
        self.assertEqual(report.returns.skipped, ())
        self.assertEqual((self.stage_writes(), len(self.sources.pending())), first)

    def test_a_wiped_queue_still_knows_the_row_from_the_journal(self):
        """本机没有第二本登记册：队列没了，日志里的来源引用仍然认识这一行。"""
        self.lend()
        self.submit()
        self.pass_once()
        first = self.stage_writes()
        self.sources.path.unlink()
        report = self.pass_once()
        self.assertEqual(report.returns.known, report.returns.scanned)
        self.assertEqual(report.returns.findings, ())
        self.assertEqual(self.stage_writes(), first)

    def test_a_wiped_queue_and_journal_cannot_replay_the_return(self):
        """队列与日志一起丢了：重新登记也不成立 —— 这张单已经不在借出中了。

        两条防线一起看：发现侧定位不到唯一一张**借出中**的单（可见地跳过），就算登记进去
        也会被单据状态拦住 —— 从头到尾只有一条待归还确认入口。
        """
        loan = self.lend()
        self.submit()
        self.pass_once()
        first = self.stage_writes()
        self.sources.path.unlink()
        for operation_id in self.store.ids():
            intent, _receipt = self.store.load(operation_id)
            if not hasattr(intent, 'event'):
                continue
            (self.store.root / f'{operation_id}.json').unlink()
        report = self.pass_once()
        self.assertEqual(report.returns.findings, ())
        self.assertEqual([skip.code for skip in report.returns.skipped], [Code.EVIDENCE.value])
        self.assertEqual(self.sources.pending(), ())
        self.assertEqual(self.stage_writes(), first)
        self.assertEqual(self.adapter.read_loan(loan.ref).state, State.AWAITING_RETURN)

    def test_two_open_loans_without_the_item_answer_are_not_registered(self):
        """同一借用人两笔借出中、只填借用人：不登记，并说出「需指定单据」。"""
        first = self.lend()
        self.lend('synthetic-loan-2', ITEM2)
        row = self.submit()
        report = self.pass_once()
        self.assertEqual(report.returns.findings, ())
        self.assertEqual(self.sources.pending(), ())
        self.assertEqual([skip.code for skip in report.returns.skipped],
                         [Code.EVIDENCE.value])
        text = '\n'.join(format_return_lines(report.returns))
        self.assertIn(f'row={row.resource_id}', text)
        self.assertIn('需在归还表单填「归还物品」指定单据', text)
        # 两张单都没被推进：证据不足不是判据。
        self.assertEqual(self.adapter.read_loan(first.ref).state, State.BORROWED)

    def test_the_item_answer_routes_the_return_to_that_loan(self):
        """填了「归还物品」：定位到那一张并推进，另一张动都不动。"""
        first = self.lend()
        second = self.lend('synthetic-loan-2', ITEM2)
        row = self.submit(item=ITEM2.resource_id)
        report = self.pass_once()
        self.assertEqual([finding.loan_ref for finding in report.returns.findings],
                         [second.ref])
        self.assertEqual(self.adapter.read_loan(second.ref).state, State.AWAITING_RETURN)
        self.assertEqual(self.adapter.read_loan(first.ref).state, State.BORROWED)
        self.assertEqual(self.sources.pending()[0].source, row)

    def test_history_before_the_watermark_is_never_registered(self):
        """水位之前的归还行：不登记、不推待办，单独记成历史行。"""
        loan = self.lend()
        old = self.submit('synthetic-return-row-old', occurred=HISTORIC)
        fresh = self.submit('synthetic-return-row-new')
        report = self.pass_once()
        self.assertEqual(report.returns.scanned, 2)
        self.assertEqual(report.returns.history, (old.resource_id,))
        self.assertEqual([finding.source.resource_id for finding in report.returns.findings],
                         [fresh.resource_id])
        self.assertEqual([item.source.resource_id for item in self.sources.pending()],
                         [fresh.resource_id])
        self.assertEqual(self.adapter.read_loan(loan.ref).state, State.AWAITING_RETURN)
        text = '\n'.join(format_return_lines(report.returns))
        self.assertIn('历史行（归还时间在水位之前，不登记）', text)

    def test_no_watermark_registers_nothing_and_says_what_to_configure(self):
        """不配水位 = 只出报告（dry-run）：表里有行也不得登记一条。"""
        self.lend()
        self.submit('synthetic-return-row-1')
        self.submit('synthetic-return-row-2', occurred=HISTORIC)
        report = self.pass_once(since=None)
        self.assertEqual(report.returns.scanned, 2)
        self.assertEqual(report.returns.findings, ())
        self.assertEqual([skip.code for skip in report.returns.skipped],
                         [Code.CONFIG.value] * 2)
        self.assertEqual(self.sources.pending(), ())
        self.assertEqual(self.stage_writes(), (0, 0))
        text = '\n'.join(format_return_lines(report.returns))
        self.assertIn('水位未配置', text)
        self.assertIn('return_intake.since', text)

    def test_a_shape_change_is_not_read_as_an_empty_table(self):
        """``records: null`` 而没有 ``hasMore``：fail closed，不读成「今天没有归还」。"""
        self.lend()
        self.submit()
        self.transport.list_payload = {'records': None}
        report = self.pass_once()
        self.assertEqual(report.returns.scan_code, Code.EVIDENCE.value)
        self.assertEqual(report.returns.scanned, 0)
        self.assertEqual(report.returns.findings, ())
        self.assertEqual(self.sources.pending(), ())
        self.assertEqual(self.stage_writes(), (0, 0))
        text = '\n'.join(format_return_lines(report.returns))
        self.assertIn('本次回读 0 行', text)
        self.assertIn('本轮未登记任何归还', text)
        self.assertNotIn('结果表当前确实为空', text)

    def test_an_unreadable_row_is_a_visible_skip_and_registers_nothing(self):
        self.lend()
        row = self.submit()
        self.transport.drop_commands = {'record.query'}
        report = self.pass_once()
        self.assertEqual(report.returns.scanned, 1)
        self.assertEqual(report.returns.findings, ())
        self.assertEqual([skip.row_id for skip in report.returns.skipped],
                         [row.resource_id])
        self.assertEqual([skip.code for skip in report.returns.skipped],
                         [Code.UNKNOWN.value])
        self.assertEqual(self.sources.pending(), ())
        self.assertEqual(self.stage_writes(), (0, 0))

    def test_entry_rows_are_reported_apart_from_return_rows(self):
        """入口行既不是跳过原因也不是失败，但不能混进归还的账目里。"""
        loan = self.lend()
        entry = Resource('form', 'synthetic-org', ENTRY_CONTAINER, 'synthetic-entry-row')
        self.transport.seed_record(entry, entry_row_cells(loan.ref))
        report = self.pass_once()
        self.assertEqual(report.returns.entry_rows, (entry.resource_id,))
        self.assertEqual(report.returns.findings, ())
        self.assertEqual(report.returns.skipped, ())
        text = '\n'.join(format_return_lines(report.returns))
        self.assertIn('非归还行（阶段入口行', text)

    def test_the_return_line_does_not_overwrite_the_application_line(self):
        """两条发现线各出一行：归还的账目不冲掉申请的账目。"""
        self.lend()
        self.submit()
        report = self.pass_once(applications=True)
        text = '\n'.join(format_drive_lines(report))
        self.assertIn('申请发现：扫描', text)
        self.assertIn('归还发现：扫描 1，本机已有 0，本轮登记 1，跳过 0', text)
        self.assertNotEqual(report.intake.scanned, report.returns.scanned)

    def test_the_reports_only_carry_row_ids_containers_and_codes(self):
        """脱敏：报告里没有人名、物品名，也没有台账单号。"""
        loan = self.lend()
        row = self.submit()
        report = self.pass_once()
        text = '\n'.join(format_return_lines(report.returns))
        self.assertIn(f'row={row.resource_id}', text)
        self.assertIn(f'container={ENTRY_CONTAINER}', text)
        self.assertNotIn(BORROWER.user_id, text)
        self.assertNotIn(loan.ref.resource_id, text)
        self.assertNotIn('SYNTHETIC-万用表', text)
