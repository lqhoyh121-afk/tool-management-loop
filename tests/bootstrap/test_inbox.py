"""轮询自动发现新申请：幂等 / fail closed / 脱敏（issue #78）。SYNTHETIC 端口，无真机。

这些用例只证明三件事，而且都用可独立核对的事实：

* 真人提交一行后**不需要**操作员登记，同一轮驱动就出现审批入口与审批待办；
* 同一行扫两次只多一条（台账行一条、队列引用一条、入口一个）；
* 读不全 / 必填缺失 / 字段映射没配 / 重复行 / 上次写入结果不明 —— 一律跳过并给出
  原因码，且报告文本里不出现人名。
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

    def add_row(self, row_id, quantity=2, physical_ids=(), fail=None, listed=True):
        self.rows[row_id] = {'quantity': quantity, 'physical_ids': tuple(physical_ids),
                             'fail': fail}
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
            self.borrower, row['quantity'], row['physical_ids'], self.occurred, self.due)

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

    def create_stage(self, request, binding, lease):
        source = Resource('form', request.loan.ref.tenant_id, 'synthetic-forms',
                          f'synthetic-entry-{len(self.entries) + 1}')
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


class IntakeHarness:
    def __init__(self, runtime, lock_root):
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
        self.intake = ApplicationIntake(self.engine, self.port, self.sources, self.store,
                                        self.locks, self.runtime / REGISTER_NAME)

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
