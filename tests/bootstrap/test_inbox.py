"""轮询自动发现新申请：幂等 / fail closed / 脱敏（issue #78）。SYNTHETIC 端口，无真机。

这些用例只证明三件事，而且都用可独立核对的事实：

* 真人提交一行后**不需要**操作员登记，同一轮驱动就出现审批入口与审批待办；
* 同一行扫两次只多一条（台账行一条、队列引用一条、入口一个）；
* 读不全 / 必填缺失 / 字段映射没配 / 重复行 / 上次写入结果不明 —— 一律跳过并给出
  原因码，且报告文本里不出现人名。
"""
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t04_binding_doc import binding_document

from bootstrap.binding import save_binding
from bootstrap.drive import DriveLoop, FileSources, format_drive_lines
from bootstrap.inbox import (REGISTER_NAME, ApplicationIntake, format_intake_lines)
from bootstrap.instance import MachineLock
from bootstrap.journal import FileJournal
from contracts.flow import Outcome
from contracts.model import (Action, Code, ContractError, Event, Identity, Resource,
                             State)
from contracts.ports import LedgerScope, RuntimeBinding, StageReceipt, stage_operation_id
from integrations.dingtalk.application import ApplicationDraft, application_marker
from workflow.engine import LendingEngine


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / 'contracts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = _load('t78_inbox_fixtures', 'fixtures.py')

BINDING = RuntimeBinding(
    fixtures.MANAGER, LedgerScope.from_record(fixtures.ITEM), 'synthetic-config-v1',
    fixtures.MANAGER, fixtures.MANAGER, 'synthetic-binding-readback', True, True, True, True)
LOAN_CONTAINER = 'synthetic-auto-loans'
#: 启用水位（#78 第 2 条）：早于所有替身申请行的申请时间，所以默认情况下每行都算新申请。
WATERMARK = fixtures.NOW - timedelta(days=2)
#: 水位之前的历史申请：启用当天表里通常已经有这种行，不能当新申请建行。
HISTORIC = fixtures.NOW - timedelta(days=5)


class FakeLedger:
    """台账读侧替身：只有本进程里登记过的行读得出来。"""

    def __init__(self):
        self.loans = {}
        self.events = {}

    def add(self, loan, source, event):
        self.loans[loan.ref] = loan
        self.events[(loan.ref, source)] = event

    def read_loan(self, ref):
        loan = self.loans.get(ref)
        if loan is None:
            raise ContractError(Code.WRONG_LOAN)
        return loan

    def read_inventory(self, ref):
        raise ContractError(Code.WRONG_LOAN)

    def read_event(self, loan, source):
        event = self.events.get((loan.ref, source))
        if event is None:
            raise ContractError(Code.EVIDENCE)
        return event


class FakeInbox:
    """申请端口替身：扫描 / 读行 / 建台账行 / 认领。

    替身刻意只做真机做过的事：读不到的格子就报缺字段，写入结果读不回来就报
    ``WRITE_UNKNOWN_QUERY_FIRST``，而且行已经落在台账里（下一次靠链路键认领）。
    """

    def __init__(self, ledger, container='synthetic-apply-forms',
                 loan_container=LOAN_CONTAINER):
        self.ledger = ledger
        self.application_container = container
        self.loan_container = loan_container
        self.rows = {}
        self.listing = []
        self.scan_error = None
        self.pre_create_error = None
        self.post_create_error = None
        self.find_error = None
        self.missing_item_map = False
        self.wrong_approver = False
        self.borrower = fixtures.BORROWER
        self.occurred = fixtures.NOW - timedelta(days=1)
        self.due = fixtures.NOW + timedelta(days=2)
        self.created = []
        self.create_calls = 0
        self.find_calls = 0

    def add_row(self, row_id, quantity=2, physical_ids=(), fail=None, listed=True,
                occurred=None):
        self.rows[row_id] = {'quantity': quantity, 'physical_ids': tuple(physical_ids),
                             'fail': fail,
                             'occurred': self.occurred if occurred is None else occurred}
        if listed:
            self.listing.append(row_id)
        return row_id

    def pending_applications(self, tenant_id):
        if self.scan_error is not None:
            raise ContractError(self.scan_error)
        return tuple(Resource('form', tenant_id, self.application_container, row_id)
                     for row_id in self.listing)

    def read_application(self, source):
        row = self.rows.get(source.resource_id)
        if row is None:
            raise ContractError(Code.EVIDENCE)
        if row['fail'] is not None:
            raise ContractError(row['fail'])
        if self.missing_item_map:
            raise ContractError(Code.CONFIG)
        return ApplicationDraft(
            source,
            Resource('record', source.tenant_id, 'synthetic-stock', 'synthetic-item'),
            self.borrower, row['quantity'], row['physical_ids'], row['occurred'], self.due)

    def create_application_loan(self, draft, binding, lease):
        self.create_calls += 1
        if self.pre_create_error is not None:
            raise ContractError(self.pre_create_error)
        ref = self._write(draft, binding)
        if self.post_create_error is not None:
            raise ContractError(self.post_create_error)
        return ref

    def find_application_loan(self, source, lease):
        self.find_calls += 1
        if self.find_error is not None:
            raise ContractError(self.find_error)
        for ref, loan in self.ledger.loans.items():
            if loan.application_evidence == application_marker(source):
                return ref
        return None

    def _write(self, draft, binding):
        ref = Resource('record', draft.source.tenant_id, self.loan_container,
                       f'synthetic-auto-loan-{len(self.created) + 1}')
        loan = draft.loan(ref, binding)
        if self.wrong_approver:
            # 写入没有把审批人落成绑定里那个人：受理侧必须自己发现，不能拿台账当绑定的回声。
            loan = replace(loan, approver=self.borrower)
        event = Event(Action.APPLY, f'{draft.source.resource_id}:apply', ref, draft.source,
                      draft.borrower, draft.occurred_at, binding.config_version, 'form', True,
                      quantity=draft.quantity, physical_ids=draft.physical_ids,
                      evidence_ref=application_marker(draft.source))
        self.ledger.add(loan, draft.source, event)
        self.created.append(ref)
        return ref


class FakeStages:
    """阶段入口与审批待办替身：新建入口行记一条，审批阶段另发一条审批待办。"""

    def __init__(self):
        self.entries = {}
        self.approver_todos = []
        self.create_calls = 0

    def create_stage(self, request, binding, lease):
        self.create_calls += 1
        source = Resource('form', request.loan.ref.tenant_id, 'synthetic-forms',
                          f'synthetic-entry-{self.create_calls}')
        receipt = StageReceipt(request.operation_id, Outcome.VERIFIED, source, None,
                               'synthetic-stage-create', 'synthetic-stage-readback')
        self.entries[request.operation_id] = receipt
        if request.action == Action.APPROVE:
            self.approver_todos.append(request.operation_id)
        return receipt

    def query_stage(self, request):
        return self.entries.get(request.operation_id,
                                StageReceipt(request.operation_id, Outcome.UNKNOWN))


class WriterStub:
    """apply 路径不写台账；真被调用就是用例写错了。"""

    def submit(self, intent, binding, lease):
        raise AssertionError('申请受理不应在台账上提交写入')


class ExplodingIntake:
    """发现阶段自己炸掉的替身：整轮必须照常跑完（#78 第 4 条）。"""

    def __init__(self, code=None):
        self.code = code

    def run(self):
        if self.code is None:
            raise RuntimeError('synthetic discovery blew up')
        raise ContractError(self.code)


class IntakeHarness:
    def __init__(self, runtime, lock_root, since=WATERMARK, intake=None):
        self.runtime = Path(runtime)
        self.locks = MachineLock(lock_root)
        self.store = FileJournal(self.runtime / 'operations')
        self.ledger = FakeLedger()
        self.port = FakeInbox(self.ledger)
        self.stages = FakeStages()
        self.sources = FileSources(self.runtime / 'sources.json')
        self.engine = LendingEngine(self.ledger, WriterStub(), self.stages, self.store,
                                    self.locks, BINDING)
        self.engine.start(BINDING.ledger, BINDING.account)
        self.intake = intake if intake is not None else ApplicationIntake(
            self.engine, self.port, self.sources, self.store, self.locks,
            self.runtime / REGISTER_NAME, since=since)

    def pass_once(self):
        return DriveLoop(self.engine, self.sources, self.store, self.locks,
                         intake=self.intake).run()

    def queue(self):
        return self.sources.pending()

    def register_rows(self):
        path = self.runtime / REGISTER_NAME
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding='utf-8'))['rows']

    def stop(self):
        if self.engine.lease is not None:
            self.engine.stop()


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.runtime = self.root / 'runtime'
        self.lock_root = self.root / 'locks'
        save_binding(self.runtime, binding_document())
        self.harness = IntakeHarness(self.runtime, self.lock_root)

    def tearDown(self):
        self.harness.stop()
        self._temp.cleanup()

    def test_submitted_row_reaches_approval_in_one_pass_without_registration(self):
        self.harness.port.add_row('synthetic-apply-0001')
        report = self.harness.pass_once()
        self.assertEqual(report.intake.scanned, 1)
        self.assertEqual(report.intake.known, 0)
        self.assertEqual(len(report.intake.findings), 1)
        self.assertEqual(report.intake.skipped, ())
        # 台账行一条，队列引用一条，登记册记到这一行。
        self.assertEqual(len(self.harness.port.created), 1)
        self.assertEqual(len(self.harness.queue()), 1)
        self.assertEqual(self.harness.queue()[0].kind, 'apply')
        self.assertEqual(
            self.harness.register_rows()['synthetic-apply-0001']['status'], 'registered')
        # 同一轮就出现审批入口与审批待办，不需要操作员登记。
        loan_ref = self.harness.port.created[0]
        self.assertEqual(self.harness.ledger.read_loan(loan_ref).state,
                         State.AWAITING_APPROVAL)
        self.assertEqual(len(self.harness.stages.entries), 1)
        self.assertEqual(len(self.harness.stages.approver_todos), 1)
        self.assertEqual([item.kind for item in report.processed], ['apply'])

    def test_rescan_of_the_same_row_creates_nothing_and_keeps_visibility(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.pass_once()
        second = self.harness.pass_once()
        self.assertEqual(second.intake.scanned, 1)
        self.assertEqual(second.intake.known, 1)
        self.assertEqual(second.intake.findings, ())
        self.assertEqual(len(self.harness.port.created), 1)
        self.assertEqual(len(self.harness.queue()), 1)
        self.assertEqual(len(self.harness.stages.entries), 1)
        self.assertEqual(self.harness.port.create_calls, 1)

    def test_restart_does_not_rebuild_the_row_or_the_entry(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.pass_once()
        self.harness.stop()
        restarted = IntakeHarness(self.runtime, self.lock_root)
        restarted.port.rows = self.harness.port.rows
        restarted.port.listing = self.harness.port.listing
        try:
            report = DriveLoop(restarted.engine, restarted.sources, restarted.store,
                               restarted.locks, intake=restarted.intake).run()
            self.assertEqual(report.intake.findings, ())
            self.assertEqual(report.intake.known, 1)
            self.assertEqual(restarted.port.create_calls, 0)
            self.assertEqual(len(restarted.sources.pending()), 1)
        finally:
            restarted.stop()

    def test_row_without_a_required_cell_is_skipped_and_reported(self):
        self.harness.port.add_row('synthetic-apply-0001', fail=Code.QUANTITY)
        report = self.harness.pass_once()
        self.assertEqual(report.intake.skipped[0].code, Code.QUANTITY.value)
        self.assertEqual(self.harness.port.created, [])
        self.assertEqual(self.harness.queue(), ())
        self.assertIn('row=synthetic-apply-0001 QUANTITY_MISMATCH',
                      '\n'.join(format_drive_lines(report)))

    def test_row_without_a_declared_field_map_is_skipped_as_config(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.port.missing_item_map = True
        report = self.harness.pass_once()
        self.assertEqual(report.intake.skipped[0].code, Code.CONFIG.value)
        self.assertEqual(self.harness.port.created, [])

    def test_duplicate_row_in_one_scan_is_skipped_once_but_still_registers_one(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.port.listing.append('synthetic-apply-0001')
        report = self.harness.pass_once()
        self.assertEqual(len(report.intake.findings), 1)
        self.assertEqual([skip.code for skip in report.intake.skipped],
                         [Code.DUPLICATE.value])
        self.assertEqual(len(self.harness.port.created), 1)
        self.assertEqual(len(self.harness.queue()), 1)

    def test_unreadable_scan_registers_nothing_and_says_so(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.port.scan_error = Code.EVIDENCE
        report = self.harness.pass_once()
        self.assertEqual(report.intake.scan_code, Code.EVIDENCE.value)
        self.assertEqual(report.intake.scanned, 0)
        self.assertEqual(report.intake.findings, ())
        self.assertEqual(self.harness.port.created, [])
        self.assertIn('未读到申请表', '\n'.join(format_intake_lines(report.intake)))

    def test_creation_with_an_unreadable_result_is_adopted_never_resent(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.port.post_create_error = Code.UNKNOWN
        first = self.harness.pass_once()
        self.assertEqual(first.intake.skipped[0].code, Code.UNKNOWN.value)
        self.assertEqual(len(self.harness.port.created), 1)
        self.assertEqual(self.harness.queue(), ())
        second = self.harness.pass_once()
        self.assertEqual(len(second.intake.findings), 1)
        self.assertEqual(self.harness.port.create_calls, 1)
        self.assertEqual(len(self.harness.port.created), 1)
        self.assertEqual(len(self.harness.queue()), 1)
        self.assertEqual(len(self.harness.stages.entries), 1)

    def test_creation_with_no_adoptable_row_is_reported_and_not_repeated(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.port.pre_create_error = Code.UNKNOWN
        self.harness.pass_once()
        second = self.harness.pass_once()
        self.assertEqual(second.intake.skipped[0].code, Code.UNKNOWN.value)
        self.assertEqual(self.harness.port.create_calls, 1)
        self.assertEqual(self.harness.queue(), ())

    def test_adoption_that_cannot_be_read_back_is_skipped(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.port.post_create_error = Code.UNKNOWN
        self.harness.pass_once()
        self.harness.port.find_error = Code.EVIDENCE
        second = self.harness.pass_once()
        self.assertEqual(second.intake.skipped[0].code, Code.EVIDENCE.value)
        self.assertEqual(self.harness.queue(), ())

    def test_discovery_needs_the_machine_lease(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.stop()
        with self.assertRaises(ContractError) as raised:
            self.harness.intake.run()
        self.assertEqual(raised.exception.code, Code.INSTANCE)
        self.assertEqual(self.harness.port.created, [])

    def test_created_row_must_still_match_the_binding(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.port.wrong_approver = True
        report = self.harness.pass_once()
        self.assertEqual(report.intake.skipped[0].code, Code.CONFIG.value)
        self.assertEqual(self.harness.queue(), ())
        # 行已经落在台账里：登记册停在「正在建」，下一次只认领，不重发。
        self.assertEqual(
            self.harness.register_rows()['synthetic-apply-0001']['status'], 'creating')

    def test_no_watermark_means_report_only_never_a_row(self):
        """没有水位时一行都不建（#78 第 2 条）：首次启用不得把表里的历史行全建成台账行。

        表里 3 行（含一行历史行）时默认行为必须保守：扫描照报，但台账行 / 队列引用 /
        审批入口 / 审批待办一个都不许出现。
        """
        self.harness.stop()
        self.harness = IntakeHarness(self.runtime, self.lock_root, since=None)
        port = self.harness.port
        port.add_row('synthetic-apply-0001')
        port.add_row('synthetic-apply-0002', occurred=HISTORIC)
        port.add_row('synthetic-apply-0003')
        report = self.harness.pass_once()
        self.assertEqual(report.intake.scanned, 3)
        self.assertEqual([skip.code for skip in report.intake.skipped],
                         [Code.CONFIG.value] * 3)
        self.assertEqual(report.intake.findings, ())
        self.assertEqual(report.intake.history, ())
        self.assertEqual(port.create_calls, 0)
        self.assertEqual(port.find_calls, 0)
        self.assertEqual(self.harness.ledger.loans, {})
        self.assertEqual(self.harness.queue(), ())
        self.assertEqual(self.harness.stages.entries, {})
        self.assertEqual(self.harness.register_rows(), {})
        text = '\n'.join(format_intake_lines(report.intake))
        self.assertIn('水位未配置', text)
        self.assertIn('回读 3 行', text)

    def test_history_before_the_watermark_is_reported_not_registered(self):
        """水位之前的行是启用前就存在的历史行：不建行、不登记、不推待办。"""
        self.harness.port.add_row('synthetic-apply-0002', occurred=HISTORIC)
        self.harness.port.add_row('synthetic-apply-0001')
        report = self.harness.pass_once()
        self.assertEqual(report.intake.scanned, 2)
        self.assertEqual(report.intake.history, ('synthetic-apply-0002',))
        self.assertEqual([finding.source.resource_id for finding in report.intake.findings],
                         ['synthetic-apply-0001'])
        self.assertEqual(len(self.harness.port.created), 1)
        # 历史行连一次台账侧查找都不做：水位不靠台账行判断，所以也没有额外读放大。
        self.assertEqual(self.harness.port.find_calls, 1)
        self.assertEqual([item.source.resource_id for item in self.harness.queue()],
                         ['synthetic-apply-0001'])
        self.assertNotIn('synthetic-apply-0002', self.harness.register_rows())
        self.assertEqual(len(self.harness.stages.entries), 1)
        text = '\n'.join(format_intake_lines(report.intake))
        self.assertIn('历史行', text)
        self.assertIn('synthetic-apply-0002', text)

    def test_wiped_local_state_adopts_instead_of_building_a_second_row(self):
        """删掉本机登记册与队列后重跑：靠台账侧链路键认领，不得多出台账行/入口/待办。

        这就是复核探针当时拿到 ``create_calls=2``、台账 2 行的那个场景。台账行在平台
        上还在、登记册与队列没了，所以只允许**认领**（建行前先查链路键），不允许重发。
        """
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.pass_once()
        self.assertEqual(self.harness.port.create_calls, 1)
        (self.runtime / 'sources.json').unlink()
        (self.runtime / REGISTER_NAME).unlink()
        report = self.harness.pass_once()
        self.assertEqual(report.intake.scanned, 1)
        self.assertEqual(report.intake.history, ())
        self.assertEqual(len(report.intake.findings), 1)
        self.assertEqual(self.harness.port.create_calls, 1)
        self.assertEqual(len(self.harness.ledger.loans), 1)
        self.assertEqual(len(self.harness.port.created), 1)
        # 队列引用、审批入口、审批待办也都只有第一条。
        self.assertEqual(len(self.harness.queue()), 1)
        self.assertEqual(self.harness.stages.create_calls, 1)
        self.assertEqual(len(self.harness.stages.approver_todos), 1)
        self.assertEqual(
            self.harness.register_rows()['synthetic-apply-0001']['status'], 'registered')

    def test_known_gap_a_wiped_operation_journal_recreates_the_stage_entry(self):
        """已知缺口（**不在 #78 范围**）：删掉 ``runtime/operations/`` 会补建第二个阶段入口。

        阶段入口的幂等判据是 ``runtime/operations/<operation_id>.json`` 里的写入意图
        （#68/#72 的设计），不是台账行、也不是申请侧的链路键：日志被删，「已建过的入口」
        就没了判据，阶段对账会再补一条审批入口与审批待办。这是这条流水线**已存在**的缺口，
        与 #78 的重复建行同源不同层，应当单独成卡；这条用例把它钉住（绿灯即现状），
        修完那条卡之后这里应当改成 ``approver_todos == 1``。
        """
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.pass_once()
        shutil.rmtree(self.runtime / 'operations')
        (self.runtime / 'operations').mkdir()
        (self.runtime / 'sources.json').unlink()
        (self.runtime / REGISTER_NAME).unlink()
        report = self.harness.pass_once()
        # #78 修好的那一层：台账行不重复建（按链路键认领）。
        self.assertEqual(self.harness.port.create_calls, 1)
        self.assertEqual(len(self.harness.ledger.loans), 1)
        self.assertEqual(len(report.intake.findings), 1)
        # 日志被删那一层：入口与待办各多一条（现状，等单独那张卡）。
        self.assertEqual(self.harness.stages.create_calls, 2)
        self.assertEqual(len(self.harness.stages.approver_todos), 2)

    def test_a_broken_discovery_does_not_take_the_pass_with_it(self):
        """发现阶段抛异常（非 ContractError）：整轮照跑，阶段对账与队列消费都在。"""
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.pass_once()
        self.harness.intake = ExplodingIntake()
        report = self.harness.pass_once()
        self.assertEqual(report.intake.scan_code, Code.UNKNOWN.value)
        self.assertEqual(report.intake.scanned, 0)
        self.assertEqual(report.intake.findings, ())
        # 整轮还在跑：阶段对账检查了已有的单，队列/阶段条目照旧被这一轮处理（重复按跳过记账）。
        self.assertTrue(report.stage_checked)
        self.assertTrue(report.skipped or report.processed)
        self.assertEqual(len(self.harness.stages.entries), 1)
        text = '\n'.join(format_drive_lines(report))
        self.assertIn('发现扫描抛异常', text)
        self.assertIn(Code.UNKNOWN.value, text)

    def test_a_discovery_contract_failure_is_recorded_not_raised(self):
        """发现的 ContractError（非实例闸门）同样只记账：本轮未登记，整轮继续。"""
        self.harness.intake = ExplodingIntake(Code.EVIDENCE)
        report = self.harness.pass_once()
        self.assertEqual(report.intake.scan_code, Code.EVIDENCE.value)
        self.assertEqual(report.intake.findings, ())

    def test_only_the_instance_gate_may_stop_the_whole_pass(self):
        """实例闸门是唯一的例外：它必须带着整轮一起停，不能被记成一次跳过。"""
        self.harness.intake = ExplodingIntake(Code.INSTANCE)
        with self.assertRaises(ContractError) as raised:
            self.harness.pass_once()
        self.assertEqual(raised.exception.code, Code.INSTANCE)

    def test_report_text_carries_no_personal_values(self):
        self.harness.port.add_row('synthetic-apply-0001', quantity=7)
        report = self.harness.pass_once()
        text = '\n'.join(format_intake_lines(report.intake))
        self.assertIn('synthetic-apply-0001', text)
        self.assertNotIn(fixtures.BORROWER.user_id, text)
        self.assertNotIn('7', text)
        self.assertIn('申请发现：', '\n'.join(format_drive_lines(report)))

    def test_register_mapping_stays_in_the_runtime_directory(self):
        self.harness.port.add_row('synthetic-apply-0001')
        self.harness.pass_once()
        rows = self.harness.register_rows()
        entry = rows['synthetic-apply-0001']
        self.assertEqual(entry['status'], 'registered')
        self.assertEqual(entry['loan']['resource_id'],
                         self.harness.port.created[0].resource_id)
        self.assertEqual((self.runtime / REGISTER_NAME).parent, self.runtime)


if __name__ == '__main__':
    unittest.main()
