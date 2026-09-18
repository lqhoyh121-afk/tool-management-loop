"""申请行的自动发现/建行/认领走真适配器与内存替身（issue #78）。

这一层回答三个只有真读/写路径才能回答的问题：

* 扫描出来的行、建出来的台账行，形态是不是**真机回读**的形态（不是替身自造）；
* 写入结果读不回来时是否 fail closed（报 ``WRITE_UNKNOWN_QUERY_FIRST`` 并按链路键
  认领，而不是重发一次新建）；
* 一轮驱动里真人提交的行是否真的自己走到审批入口与审批待办。
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_memory_transport import MemoryTransport

from bootstrap.drive import DriveLoop, FileSources
from bootstrap.inbox import (REGISTER_NAME, ApplicationIntake, format_intake_lines)
from bootstrap.instance import MachineLock
from bootstrap.journal import FileJournal
from contracts.model import Code, ContractError, Resource, State
from contracts.ports import LedgerScope, RuntimeBinding
from integrations.dingtalk.adapter import DingTalkAdapter, TitleDisplay
from integrations.dingtalk.application import application_marker
from integrations.dingtalk.codec import _put_identity, encode_loan
from integrations.dingtalk.dws_transport import DwsTransport
from integrations.dingtalk.layout import (ApplicationFieldMap, SYNTHETIC_APPLY_FIELDS,
                                          SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_FIELDS,
                                          SYNTHETIC_RETURN_FORM_FIELDS)
from workflow.engine import LendingEngine


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
fixtures = _load('t78_intake_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
synthetic = _load('t78_intake_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')

BORROWER, ITEM, NOW = fixtures.BORROWER, fixtures.ITEM, fixtures.NOW
MANAGER = fixtures.MANAGER
APPLY_CONTAINER = 'synthetic-apply-forms'
LOAN_CONTAINER = 'synthetic-loans'
#: 启用水位（#78 第 2 条）：早于替身申请行的申请时间。
WATERMARK = NOW - timedelta(days=3)
#: 水位之前的历史申请：启用当天表里往往已经有这种行。
HISTORIC = NOW - timedelta(days=5)


class ShapeChangingTransport(MemoryTransport):
    """内存替身 + 能把申请扫描的报文换成一个「读不到」的形态。

    真机上 ``record query --all`` 在表为空时回 ``records: null`` 且 ``hasMore: false``；
    形态变了（或查询失败回了 null）是另一回事，必须 fail closed，不能被读成空表。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.list_payload = None

    def exchange(self, command, arguments):
        if command == 'row.list' and self.list_payload is not None:
            self.calls.append((command, dict(arguments)))
            return self.list_payload
        return super().exchange(command, arguments)


class IntakeAdapterTests(unittest.TestCase):
    """适配器侧：扫描 / 读行 / 建行 / 认领。"""

    def setUp(self):
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = SYNTHETIC_ENTRY_FIELDS
        self.apply_fields = SYNTHETIC_APPLY_FIELDS
        self.journal = synthetic.SyntheticJournal()
        self.leases = synthetic.SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = MemoryTransport(
            self.fields, self.entry_fields, apply_container=APPLY_CONTAINER,
            apply_fields=self.apply_fields)
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            apply_fields=self.apply_fields, application_container=APPLY_CONTAINER,
            return_form_fields=SYNTHETIC_RETURN_FORM_FIELDS,
            entry_container='synthetic-forms', loan_container=LOAN_CONTAINER)

    def submit(self, row_id='synthetic-apply-row-1', quantity='2', due_at=None,
               physical_ids=(), item_id=None):
        cells = {
            self.apply_fields.borrower: _put_identity(BORROWER),
            self.apply_fields.quantity: quantity,
            self.apply_fields.physical_ids: json.dumps(list(physical_ids), ensure_ascii=True),
            self.apply_fields.occurred_at: (NOW - timedelta(days=1)).isoformat(),
            self.apply_fields.item_container: ITEM.container_id,
            self.apply_fields.item_id: item_id or ITEM.resource_id,
            self.apply_fields.due_at: (due_at or NOW + timedelta(days=2)).isoformat(),
        }
        ref = Resource('form', 'synthetic-org', APPLY_CONTAINER, row_id)
        self.transport.seed_record(ref, cells)
        return ref

    def test_scan_returns_row_refs_and_an_empty_table_is_empty(self):
        self.assertEqual(self.adapter.pending_applications('synthetic-org'), ())
        row = self.submit()
        self.assertEqual(self.adapter.pending_applications('synthetic-org'), (row,))

    def test_row_reads_into_a_draft_with_the_marker_of_its_source(self):
        row = self.submit()
        draft = self.adapter.read_application(row)
        self.assertEqual(draft.source, row)
        self.assertEqual(draft.marker, f'form:{row.resource_id}')
        self.assertEqual(draft.quantity, 2)
        self.assertEqual(draft.item, ITEM)
        self.assertEqual(draft.borrower, BORROWER)
        self.assertFalse(draft.tracked)

    def test_unmapped_item_question_fails_closed(self):
        row = self.submit()
        adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            apply_fields=ApplicationFieldMap(quantity=self.apply_fields.quantity,
                                             physical_ids=self.apply_fields.physical_ids,
                                             borrower=self.apply_fields.borrower,
                                             occurred_at=self.apply_fields.occurred_at),
            application_container=APPLY_CONTAINER, loan_container=LOAN_CONTAINER)
        with self.assertRaises(ContractError) as raised:
            adapter.read_application(row)
        self.assertEqual(raised.exception.code, Code.CONFIG)

    def test_row_without_a_required_cell_fails_closed(self):
        row = self.submit()
        cells = self.transport.records[('synthetic-org', APPLY_CONTAINER, row.resource_id)]
        cells.pop(self.apply_fields.quantity)
        with self.assertRaises(ContractError) as raised:
            self.adapter.read_application(row)
        self.assertEqual(raised.exception.code, Code.EVIDENCE)

    def test_a_row_from_another_table_is_not_read_as_an_application(self):
        row = self.submit()
        stranger = Resource('form', 'synthetic-org', 'synthetic-forms', row.resource_id)
        with self.assertRaises(ContractError) as raised:
            self.adapter.read_application(stranger)
        self.assertEqual(raised.exception.code, Code.EVIDENCE)

    def test_create_writes_the_row_and_reads_it_back(self):
        row = self.submit()
        draft = self.adapter.read_application(row)
        ref = self.adapter.create_application_loan(draft, self.binding, self.lease)
        self.assertEqual(ref.container_id, LOAN_CONTAINER)
        stored = self.adapter.read_loan(ref)
        self.assertEqual(stored.state, State.AWAITING_APPROVAL)
        self.assertEqual(stored.application_evidence, application_marker(row))
        self.assertEqual(stored.borrower, BORROWER)
        self.assertEqual(stored.approver, self.binding.approver)
        self.assertEqual(stored.manager, self.binding.manager)
        self.assertEqual(stored.quantity, 2)

    def test_create_needs_the_machine_lease(self):
        row = self.submit()
        draft = self.adapter.read_application(row)
        self.leases.release(self.lease)
        with self.assertRaises(ContractError) as raised:
            self.adapter.create_application_loan(draft, self.binding, self.lease)
        self.assertEqual(raised.exception.code, Code.INSTANCE)

    def test_a_lost_create_reply_is_unknown_not_a_second_row(self):
        row = self.submit()
        draft = self.adapter.read_application(row)
        self.transport.drop_once.append('loan.create')
        with self.assertRaises(ContractError) as raised:
            self.adapter.create_application_loan(draft, self.binding, self.lease)
        self.assertEqual(raised.exception.code, Code.UNKNOWN)

    def test_a_lost_readback_is_unknown_and_the_row_is_adopted_next_time(self):
        row = self.submit()
        draft = self.adapter.read_application(row)
        self.transport.drop_commands.add('record.query')
        with self.assertRaises(ContractError) as raised:
            self.adapter.create_application_loan(draft, self.binding, self.lease)
        self.assertEqual(raised.exception.code, Code.UNKNOWN)
        # 行已经落地：这正是「不盲重发、只认领」要覆盖的那一步。
        self.transport.drop_commands.clear()
        adopted = self.adapter.find_application_loan(row, self.lease)
        self.assertIsNotNone(adopted)
        self.assertEqual(self.adapter.read_loan(adopted).application_evidence,
                         application_marker(row))
        self.assertEqual(self.adapter.find_application_loan(
            Resource('form', 'synthetic-org', APPLY_CONTAINER, 'synthetic-other-row'),
            self.lease), None)

    def test_two_rows_with_the_same_marker_fail_closed(self):
        row = self.submit()
        marker = application_marker(row)
        for resource_id in ('synthetic-twin-a', 'synthetic-twin-b'):
            twin = replace(fixtures.loan(), application_evidence=marker)
            self.transport.seed_record(
                Resource('record', 'synthetic-org', LOAN_CONTAINER, resource_id),
                encode_loan(replace(twin, ref=Resource('record', 'synthetic-org',
                                                       LOAN_CONTAINER, resource_id)),
                            self.fields))
        with self.assertRaises(ContractError) as raised:
            self.adapter.find_application_loan(row, self.lease)
        self.assertEqual(raised.exception.code, Code.EVIDENCE)


APPLY_BASE_TABLE = 'baseApply/tblApply'
LOAN_BASE_TABLE = 'baseLoan/tblLoan'
FORM_CONTAINER = 'baseForm/tblForm'
TODO_CONTAINER = 'todoSpace/executors'
FAKE_DWS = Path(__file__).resolve().parent / 't031_fake_dws.py'


class CliIntakeTests(unittest.TestCase):
    """同一条链走真 argv（node 换成 python 替身）：扫描 / 建行 / 认领。"""

    def setUp(self):
        from t031_fake_dws import load_state, save_state
        from t03_live_cells import declared_kinds

        self.load_state, self.save_state = load_state, save_state
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.state_path = self.work / 'fake-state.json'
        state = load_state(self.state_path)
        state['kinds'] = declared_kinds(SYNTHETIC_FIELDS, SYNTHETIC_ENTRY_FIELDS,
                                        SYNTHETIC_APPLY_FIELDS)
        save_state(self.state_path, state)
        self.journal = synthetic.SyntheticJournal()
        self.leases = synthetic.SyntheticLease()
        item = Resource('record', 'synthetic-org', LOAN_BASE_TABLE, 'recItem')
        self.lease = self.leases.acquire(LedgerScope.from_record(item), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(item), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = DwsTransport(
            [sys.executable, str(FAKE_DWS)], SYNTHETIC_FIELDS,
            form_container=FORM_CONTAINER, todo_container=TODO_CONTAINER,
            work_dir=self.work / 'runtime', entry_fields=SYNTHETIC_ENTRY_FIELDS,
            extra_env={'FAKE_DWS_STATE': str(self.state_path)}, timeout=2,
        )
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, SYNTHETIC_FIELDS,
            SYNTHETIC_ENTRY_FIELDS, apply_fields=SYNTHETIC_APPLY_FIELDS,
            application_container=APPLY_BASE_TABLE,
            return_form_fields=SYNTHETIC_RETURN_FORM_FIELDS,
            entry_container=FORM_CONTAINER, loan_container=LOAN_BASE_TABLE)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, row_id='synthetic-apply-row-1'):
        state = self.load_state(self.state_path)
        state['records'][f'{APPLY_BASE_TABLE}/{row_id}'] = {
            SYNTHETIC_APPLY_FIELDS.borrower: _put_identity(BORROWER),
            SYNTHETIC_APPLY_FIELDS.quantity: '2',
            SYNTHETIC_APPLY_FIELDS.physical_ids: '[]',
            SYNTHETIC_APPLY_FIELDS.occurred_at: (NOW - timedelta(days=1)).isoformat(),
            SYNTHETIC_APPLY_FIELDS.item_container: LOAN_BASE_TABLE,
            SYNTHETIC_APPLY_FIELDS.item_id: 'recItem',
            SYNTHETIC_APPLY_FIELDS.due_at: (NOW + timedelta(days=2)).isoformat(),
        }
        # 另一个表里的行不得混进申请扫描。
        state['records'][f'{FORM_CONTAINER}/synthetic-entry-row'] = {
            SYNTHETIC_ENTRY_FIELDS.loan_id: 'synthetic-elsewhere'}
        self.save_state(self.state_path, state)
        return Resource('form', 'synthetic-org', APPLY_BASE_TABLE, row_id)

    def test_cli_scan_and_read_use_the_application_table_only(self):
        row = self.submit()
        self.assertEqual(self.adapter.pending_applications('synthetic-org'), (row,))
        draft = self.adapter.read_application(row)
        self.assertEqual(draft.quantity, 2)
        self.assertEqual(draft.item.resource_id, 'recItem')
        self.assertEqual(draft.borrower, BORROWER)

    def test_cli_scan_of_an_empty_table_is_an_empty_tuple(self):
        self.assertEqual(self.adapter.pending_applications('synthetic-org'), ())

    def test_cli_create_lands_with_the_marker_and_reads_back(self):
        row = self.submit()
        draft = self.adapter.read_application(row)
        ref = self.adapter.create_application_loan(draft, self.binding, self.lease)
        self.assertEqual(ref.container_id, LOAN_BASE_TABLE)
        state = self.load_state(self.state_path)
        self.assertEqual(state['records'][f'{LOAN_BASE_TABLE}/{ref.resource_id}']
                         [SYNTHETIC_FIELDS.application_evidence],
                         application_marker(row))
        self.assertEqual(self.adapter.read_loan(ref).state, State.AWAITING_APPROVAL)

    def test_cli_lost_create_envelope_adopts_the_row_it_left_behind(self):
        row = self.submit()
        draft = self.adapter.read_application(row)
        state = self.load_state(self.state_path)
        state['late_write'] = ['aitable record create']
        self.save_state(self.state_path, state)
        with self.assertRaises(ContractError) as raised:
            self.adapter.create_application_loan(draft, self.binding, self.lease)
        self.assertEqual(raised.exception.code, Code.UNKNOWN)
        state = self.load_state(self.state_path)
        state['late_write'] = []
        self.save_state(self.state_path, state)
        adopted = self.adapter.find_application_loan(row, self.lease)
        self.assertIsNotNone(adopted)
        self.assertEqual(self.adapter.read_loan(adopted).application_evidence,
                         application_marker(row))


class AutoIntakeDriveTests(unittest.TestCase):
    """验收主路径：真人提交一笔 → 不人工登记 → 一轮内出现审批入口与审批待办。

    整条链走真适配器（内存替身只替平台），所以「审批入口」是 ``form.create`` 写出的
    入口行、「审批待办」是发给审批人的 ``todo.create``，都由本轮真实调用产生。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(parents=True)
        self.locks = MachineLock(self.root / 'locks')
        self.store = FileJournal(self.runtime / 'operations')
        self.since = WATERMARK
        self.transport = ShapeChangingTransport(
            SYNTHETIC_FIELDS, SYNTHETIC_ENTRY_FIELDS, apply_container=APPLY_CONTAINER,
            apply_fields=SYNTHETIC_APPLY_FIELDS)
        self.adapter = DingTalkAdapter(
            self.transport, self.store, self.locks, SYNTHETIC_FIELDS,
            SYNTHETIC_ENTRY_FIELDS, apply_fields=SYNTHETIC_APPLY_FIELDS,
            application_container=APPLY_CONTAINER,
            return_form_fields=SYNTHETIC_RETURN_FORM_FIELDS,
            entry_container='synthetic-forms', loan_container=LOAN_CONTAINER)
        self.sources = FileSources(self.runtime / 'sources.json')

    def start(self):
        binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        engine = LendingEngine(self.adapter, self.adapter, self.adapter, self.store,
                               self.locks, binding)
        engine.start(binding.ledger, binding.account)
        intake = ApplicationIntake(engine, self.adapter, self.sources, self.store,
                                   self.locks, self.runtime / REGISTER_NAME,
                                   since=self.since)
        return engine, DriveLoop(engine, self.sources, self.store, self.locks, intake=intake)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, row_id='synthetic-apply-row-1', occurred=None):
        cells = {
            SYNTHETIC_APPLY_FIELDS.borrower: _put_identity(BORROWER),
            SYNTHETIC_APPLY_FIELDS.quantity: '2',
            SYNTHETIC_APPLY_FIELDS.physical_ids: '[]',
            SYNTHETIC_APPLY_FIELDS.occurred_at: (
                NOW - timedelta(days=1) if occurred is None else occurred).isoformat(),
            SYNTHETIC_APPLY_FIELDS.item_container: ITEM.container_id,
            SYNTHETIC_APPLY_FIELDS.item_id: ITEM.resource_id,
            SYNTHETIC_APPLY_FIELDS.due_at: (NOW + timedelta(days=2)).isoformat(),
        }
        ref = Resource('form', 'synthetic-org', APPLY_CONTAINER, row_id)
        self.transport.seed_record(ref, cells)
        return ref

    def test_one_pass_turns_a_submitted_row_into_an_approval_entry_and_todo(self):
        row = self.submit()
        engine, loop = self.start()
        try:
            report = loop.run()
        finally:
            engine.stop()
        self.assertEqual(len(report.intake.findings), 1)
        self.assertEqual(report.intake.skipped, ())
        # 台账行：只新建一条，且带申请链路键。
        self.assertEqual(len(self.transport.writes_of('loan.create')), 1)
        loan_ref = self.sources.pending()[0].loan_ref
        loan = self.adapter.read_loan(loan_ref)
        self.assertEqual(loan.state, State.AWAITING_APPROVAL)
        self.assertEqual(loan.application_evidence, application_marker(row))
        # 审批入口 + 审批待办：本轮真的写出去了。
        self.assertEqual(len(self.transport.writes_of('form.create')), 1)
        self.assertEqual(len(self.transport.writes_of('todo.create')), 1)
        self.assertEqual(len(report.stage_created), 1)
        # 工作队列里只有一条引用，本轮的 apply 项被处理。
        self.assertEqual(len(self.sources.pending()), 1)
        self.assertEqual(self.sources.pending()[0].source, row)
        self.assertEqual([item.kind for item in report.processed], ['apply'])

    def test_second_pass_and_a_restart_add_nothing(self):
        self.submit()
        engine, loop = self.start()
        try:
            loop.run()
        finally:
            engine.stop()
        first = (len(self.transport.writes_of('loan.create')),
                 len(self.transport.writes_of('form.create')),
                 len(self.transport.writes_of('todo.create')))
        engine, loop = self.start()
        try:
            report = loop.run()
        finally:
            engine.stop()
        self.assertEqual(report.intake.known, 1)
        self.assertEqual(report.intake.findings, ())
        self.assertEqual(
            (len(self.transport.writes_of('loan.create')),
             len(self.transport.writes_of('form.create')),
             len(self.transport.writes_of('todo.create'))), first)
        self.assertEqual(len(self.sources.pending()), 1)

    def test_empty_table_is_reported_as_empty_not_unreadable(self):
        """表真的空：报告要写明查的哪张表、回读到 0 行、确实为空（#78 第 3 条）。"""
        engine, loop = self.start()
        try:
            report = loop.run()
        finally:
            engine.stop()
        self.assertEqual(report.intake.scanned, 0)
        self.assertEqual(report.intake.scan_code, '')
        self.assertEqual(report.intake.container, APPLY_CONTAINER)
        self.assertEqual(self.transport.writes_of('loan.create'), [])
        text = '\n'.join(format_intake_lines(report.intake))
        self.assertIn(f'container={APPLY_CONTAINER}', text)
        self.assertIn('回读 0 行', text)
        self.assertIn('确实为空', text)
        self.assertNotIn('未读到申请表', text)

    def test_a_shape_change_is_not_read_as_an_empty_table(self):
        """``records: null`` 但没有 ``hasMore``：fail closed，绝不读成「今天没人申请」。"""
        self.submit()
        engine, loop = self.start()
        self.transport.list_payload = {'records': None}
        try:
            report = loop.run()
        finally:
            engine.stop()
        self.assertEqual(report.intake.scan_code, Code.EVIDENCE.value)
        self.assertEqual(report.intake.scanned, 0)
        self.assertEqual(report.intake.findings, ())
        self.assertEqual(self.transport.writes_of('loan.create'), [])
        self.assertEqual(self.sources.pending(), ())
        text = '\n'.join(format_intake_lines(report.intake))
        self.assertIn('未读到申请表', text)
        self.assertNotIn('确实为空', text)

    def test_history_before_the_watermark_is_never_built_or_registered(self):
        """水位之前的历史行：不建台账行、不登记、不推待办（#78 第 2 条）。"""
        old = self.submit('synthetic-apply-row-old', occurred=HISTORIC)
        fresh = self.submit('synthetic-apply-row-new')
        engine, loop = self.start()
        try:
            report = loop.run()
        finally:
            engine.stop()
        self.assertEqual(report.intake.scanned, 2)
        self.assertEqual(report.intake.history, (old.resource_id,))
        self.assertEqual([finding.source.resource_id for finding in report.intake.findings],
                         [fresh.resource_id])
        self.assertEqual(len(self.transport.writes_of('loan.create')), 1)
        self.assertEqual(len(self.transport.writes_of('form.create')), 1)
        self.assertEqual(len(self.transport.writes_of('todo.create')), 1)
        self.assertEqual([item.source.resource_id for item in self.sources.pending()],
                         [fresh.resource_id])

    def test_no_watermark_builds_nothing_and_says_what_to_configure(self):
        """不配水位 = 只出报告（dry-run）：表里 3 行也不得建出 3 行。"""
        self.submit('synthetic-apply-row-1')
        self.submit('synthetic-apply-row-2', occurred=HISTORIC)
        self.submit('synthetic-apply-row-3')
        self.since = None
        engine, loop = self.start()
        try:
            report = loop.run()
        finally:
            engine.stop()
        self.assertEqual(report.intake.scanned, 3)
        self.assertEqual(report.intake.findings, ())
        self.assertEqual([skip.code for skip in report.intake.skipped],
                         [Code.CONFIG.value] * 3)
        self.assertEqual(self.transport.writes_of('loan.create'), [])
        self.assertEqual(self.sources.pending(), ())
        text = '\n'.join(format_intake_lines(report.intake))
        self.assertIn('水位未配置', text)
        self.assertIn('application_intake.since', text)


#: 库存名称列的字段 ID（绑定里的 ``title_display.item_name_field``）与「工具」答案。
NAME_FIELD = 'fldSYN-stock-item-name'
TOOL = 'SYN-万用表'


class NameResolvedIntakeDriveTests(AutoIntakeDriveTests):
    """#89 的验收主路径：申请行只填「工具 / 数量 / 归还日期」，物品按名称解析。

    这一层把 ``AutoIntakeDriveTests`` 的全部端到端断言（一轮内出台账行 + 审批入口 +
    审批待办、重跑不建第二条、水位之前的行不建、空表与形态变化 fail closed）在
    **新的物品来源**上再跑一遍：申请行不再携带「容器 + 记录 id」两格，物品由「工具」
    单选的选项名在库存容器里解析出来，所以同一套幂等 / 水位 / fail-closed 断言都
    必须照样成立。
    """

    def setUp(self):
        super().setUp()
        # 库存记录必须就是绑定作用域里的那一条：解析出来的物品要能过 check_binding。
        self.transport.seed_record(ITEM, {NAME_FIELD: TOOL})
        self.adapter = DingTalkAdapter(
            self.transport, self.store, self.locks, SYNTHETIC_FIELDS,
            SYNTHETIC_ENTRY_FIELDS, apply_fields=SYNTHETIC_APPLY_FIELDS,
            application_container=APPLY_CONTAINER,
            return_form_fields=SYNTHETIC_RETURN_FORM_FIELDS,
            entry_container='synthetic-forms', loan_container=LOAN_CONTAINER,
            inventory_container=ITEM.container_id,
            title_display=TitleDisplay(item_name_field=NAME_FIELD))

    def submit(self, row_id='synthetic-apply-row-1', occurred=None, tool=TOOL):
        cells = {
            SYNTHETIC_APPLY_FIELDS.borrower: _put_identity(BORROWER),
            SYNTHETIC_APPLY_FIELDS.quantity: '2',
            SYNTHETIC_APPLY_FIELDS.physical_ids: '[]',
            SYNTHETIC_APPLY_FIELDS.occurred_at: (
                NOW - timedelta(days=1) if occurred is None else occurred).isoformat(),
            SYNTHETIC_APPLY_FIELDS.item: tool,
            SYNTHETIC_APPLY_FIELDS.due_at: (NOW + timedelta(days=2)).isoformat(),
        }
        ref = Resource('form', 'synthetic-org', APPLY_CONTAINER, row_id)
        self.transport.seed_record(ref, cells)
        return ref

    def test_the_registered_row_points_at_the_named_inventory_record(self):
        """零人工登记：一行只填「工具」也能建成台账行，且指向名称解析出来的记录。"""
        self.submit()
        engine, loop = self.start()
        try:
            report = loop.run()
        finally:
            engine.stop()
        self.assertEqual(report.intake.skipped, ())
        self.assertEqual(len(report.intake.findings), 1)
        loan = self.adapter.read_loan(self.sources.pending()[0].loan_ref)
        self.assertEqual(loan.item, ITEM)
        self.assertEqual(loan.state, State.AWAITING_APPROVAL)

    def test_a_tool_name_that_matches_nothing_is_skipped_visibly(self):
        """名称对不上库存记录：逐行可见原因码，不建行、不登记、不推待办（fail closed）。"""
        row = self.submit(tool='SYN-绝缘手套')
        engine, loop = self.start()
        try:
            report = loop.run()
        finally:
            engine.stop()
        self.assertEqual(report.intake.findings, ())
        self.assertEqual([(skip.row_id, skip.code) for skip in report.intake.skipped],
                         [(row.resource_id, Code.EVIDENCE.value)])
        self.assertEqual(self.transport.writes_of('loan.create'), [])
        self.assertEqual(self.transport.writes_of('form.create'), [])
        self.assertEqual(self.transport.writes_of('todo.create'), [])
        self.assertEqual(self.sources.pending(), ())
