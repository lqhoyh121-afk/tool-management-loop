"""归还发现的账目与守卫（issue #87）：幂等、水位、fail closed、脱敏。

这一层用端口替身把适配器与平台关掉，只问一件事：发现侧在一轮里**该登记什么、该拦下什么**。
真读/写路径的形态与「一轮内推进到待归还确认」由 ``tests/integrations/test_return_intake.py``
用真适配器回答，两边不重复。
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

from bootstrap.drive import DriveReport, FileSources, drive_exit_code, format_drive_lines, return_intake
from bootstrap.inbox import IntakeReport
from bootstrap.returns import (READ_NOTE, REGISTER_NOTES, RegisteredReturn, ReturnIntake,
                               ReturnReport, ReturnSkip, format_return_lines)
from contracts.model import Code, ContractError, Resource, State
from contracts.ports import LedgerScope, RuntimeBinding
from integrations.dingtalk.returnform import ReturnDraft


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
fixtures = _load('t87_inbox_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')

BORROWER, ITEM, NOW, MANAGER = (fixtures.BORROWER, fixtures.ITEM, fixtures.NOW,
                                fixtures.MANAGER)
OTHER = fixtures.Identity('contact', 'synthetic-org', 'synthetic-other-borrower')
ENTRY_CONTAINER = 'synthetic-forms'
LOAN_CONTAINER = 'synthetic-loans'
RETURN_ROW = 'synthetic-return-row'
RETURNED_AT = NOW - timedelta(hours=1)
WATERMARK = NOW - timedelta(days=3)
HISTORIC = NOW - timedelta(days=5)
ROW_REF = Resource('form', 'synthetic-org', ENTRY_CONTAINER, RETURN_ROW)


def _loan(state=State.BORROWED, borrower=BORROWER, resource_id=None):
    current = replace(fixtures.loan(), state=state, borrower=borrower)
    if resource_id is not None:
        current = replace(current, ref=replace(current.ref, resource_id=resource_id))
    return current


class StubEvent:
    def __init__(self, source):
        self.source = source


class StubIntent:
    def __init__(self, source):
        self.event = StubEvent(source)


class StubReceipt:
    def __init__(self, source):
        self.source = source


class FakeStore:
    """操作日志替身：只回答「有哪些 operation_id」与它们的来源引用。"""

    def __init__(self, entries=()):
        self.entries = {operation_id: (intent, receipt)
                        for operation_id, intent, receipt in entries}

    def ids(self):
        return tuple(sorted(self.entries))

    def load(self, operation_id):
        return self.entries[operation_id]


class FakeLocks:
    def __init__(self, code=None):
        self.code = code

    def assert_held(self, lease):
        if self.code is not None:
            raise ContractError(self.code)


class FakeReader:
    """台账读侧：只回答「这张单现在是什么样」。"""

    def __init__(self, loans=()):
        self.loans = {loan.ref.resource_id: loan for loan in loans}

    def read_loan(self, ref):
        if ref.resource_id not in self.loans:
            raise ContractError(Code.EVIDENCE)
        return self.loans[ref.resource_id]


class FakeEngine:
    def __init__(self, binding, reader):
        self.binding = binding
        self.reader = reader
        self.lease = 'synthetic-lease'


class FakeReturnPort:
    """归还端口替身：扫表 / 逐行读 / 定位唯一一张借出中的单。"""

    def __init__(self, container=ENTRY_CONTAINER, loan_container=LOAN_CONTAINER):
        self.entry_container = container
        self.loan_container = loan_container
        self.rows = {}
        self.listing = []
        self.scan_error = None
        self.read_calls = []

    def submit(self, row_id=RETURN_ROW, occurred=RETURNED_AT, borrower=BORROWER,
               reads_as='return', listed=True, read_error=None, loan=None,
               resolve_error=None):
        self.rows[row_id] = {
            'occurred': occurred, 'borrower': borrower, 'reads_as': reads_as,
            'read_error': read_error, 'loan': loan, 'resolve_error': resolve_error,
        }
        if listed:
            self.listing.append(row_id)

    def pending_returns(self, tenant_id):
        if self.scan_error is not None:
            raise ContractError(self.scan_error)
        return tuple(Resource('form', tenant_id, self.entry_container, row_id)
                     for row_id in self.listing)

    def read_return(self, source):
        self.read_calls.append(source.resource_id)
        row = self.rows[source.resource_id]
        if row['read_error'] is not None:
            raise ContractError(row['read_error'])
        if row['reads_as'] != 'return':
            return None
        return ReturnDraft(source, row['borrower'], row['occurred'])

    def resolve_return_form_loan(self, source, hint_ref):
        row = self.rows[source.resource_id]
        if row['resolve_error'] is not None:
            raise ContractError(row['resolve_error'])
        return row['loan']


def runtime_binding():
    return RuntimeBinding(
        MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
        MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)


class ReturnIntakeHarness(unittest.TestCase):
    """一轮归还发现的最小舞台：真 FileSources + 替身端口。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sources = FileSources(self.root / 'sources.json')
        self.locks = FakeLocks()
        self.borrowed = _loan()

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, loans=None, entries=(), port=None, since=WATERMARK):
        self.reader = FakeReader(loans if loans is not None else (self.borrowed,))
        self.engine = FakeEngine(runtime_binding(), self.reader)
        self.port = port if port is not None else FakeReturnPort()
        self.store = FakeStore(entries)
        self.intake = ReturnIntake(self.engine, self.port, self.sources, self.store,
                                   self.locks, since=since)
        return self.intake

    def one_pass(self, loans=None, entries=(), port=None, since=WATERMARK):
        return self.stage(loans, entries, port, since).run()

    def text(self, report):
        return '\n'.join(format_return_lines(report))


class RegistrationTests(ReturnIntakeHarness):
    def test_a_new_return_row_is_registered_once_as_an_event_reference(self):
        self.stage()
        self.port.submit(loan=self.borrowed.ref)
        report = self.intake.run()
        self.assertEqual(report.scanned, 1)
        self.assertEqual(report.known, 0)
        self.assertEqual(report.skipped, ())
        self.assertEqual(report.findings, (RegisteredReturn(ROW_REF, self.borrowed.ref),))
        queue = self.sources.pending()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].kind, 'event')
        self.assertEqual(queue[0].loan_ref, self.borrowed.ref)
        self.assertEqual(queue[0].source, ROW_REF)

    def test_a_known_row_is_not_registered_again(self):
        """幂等：队列里有这一行就不再登记，也不再读它。"""
        self.stage()
        self.port.submit(loan=self.borrowed.ref)
        self.intake.run()
        self.port.read_calls.clear()
        second = self.intake.run()
        self.assertEqual(second.scanned, 1)
        self.assertEqual(second.known, 1)
        self.assertEqual(second.findings, ())
        self.assertEqual(self.port.read_calls, [])
        self.assertEqual(len(self.sources.pending()), 1)

    def test_a_row_the_journal_already_mentions_is_not_registered_again(self):
        """队列丢了也不要紧：日志里的事件来源同样算「本机已有引用」。

        归还登记不产生任何平台写入，所以队列本身就是登记册 —— 这一段钉住「没有第二本
        账」也要幂等。
        """
        entries = (('synthetic-op', StubIntent(ROW_REF), StubReceipt(None)),)
        self.stage(entries=entries)
        self.port.submit(loan=self.borrowed.ref)
        report = self.intake.run()
        self.assertEqual(report.known, 1)
        self.assertEqual(report.findings, ())
        self.assertEqual(self.sources.pending(), ())

    def test_a_stage_receipt_of_the_same_table_counts_as_a_reference(self):
        entries = (('synthetic-stage', object(), StubReceipt(ROW_REF)),)
        self.stage(entries=entries)
        self.port.submit(loan=self.borrowed.ref)
        report = self.intake.run()
        self.assertEqual(report.known, 1)
        self.assertEqual(report.findings, ())

    def test_the_same_row_twice_in_one_scan_registers_once_and_is_reported(self):
        self.stage()
        self.port.submit(loan=self.borrowed.ref)
        self.port.listing.append(RETURN_ROW)
        report = self.intake.run()
        self.assertEqual(report.scanned, 2)
        self.assertEqual(len(report.findings), 1)
        self.assertEqual([skip.code for skip in report.skipped], [Code.DUPLICATE.value])
        self.assertEqual(len(self.sources.pending()), 1)

    def test_a_row_that_reads_as_something_else_is_counted_apart(self):
        """阶段入口行不是跳过原因，也不能混进归还的账目里。"""
        self.stage()
        self.port.submit('synthetic-entry-row', reads_as='entry')
        report = self.intake.run()
        self.assertEqual(report.scanned, 1)
        self.assertEqual(report.entry_rows, ('synthetic-entry-row',))
        self.assertEqual(report.skipped, ())
        self.assertEqual(self.sources.pending(), ())


class GuardTests(ReturnIntakeHarness):
    def test_a_loan_that_is_not_borrowed_anymore_is_not_registered(self):
        moved = _loan(state=State.AWAITING_RETURN)
        self.stage(loans=(moved,))
        self.port.submit(loan=moved.ref)
        report = self.intake.run()
        self.assertEqual(report.findings, ())
        self.assertEqual([skip.code for skip in report.skipped], [Code.STATE.value])
        self.assertEqual(self.sources.pending(), ())

    def test_a_loan_of_another_borrower_is_not_registered(self):
        """定位到的单必须自证「就是这一行的人借的」：换人等于换单。"""
        others = _loan(borrower=OTHER)
        self.stage(loans=(others,))
        self.port.submit(loan=others.ref)
        report = self.intake.run()
        self.assertEqual(report.findings, ())
        self.assertEqual([skip.code for skip in report.skipped], [Code.WRONG_PERSON.value])

    def test_no_declared_ledger_container_fails_closed(self):
        port = FakeReturnPort(loan_container='')
        self.stage(port=port)
        port.submit(loan=self.borrowed.ref)
        report = self.intake.run()
        self.assertEqual(report.findings, ())
        self.assertEqual([skip.code for skip in report.skipped], [Code.CONFIG.value])

    def test_a_scan_that_never_got_a_conclusion_registers_nothing(self):
        port = FakeReturnPort()
        port.scan_error = Code.EVIDENCE
        self.stage(port=port)
        port.submit(loan=self.borrowed.ref)
        report = self.intake.run()
        self.assertEqual(report.scan_code, Code.EVIDENCE.value)
        self.assertEqual(report.scanned, 0)
        self.assertEqual(report.findings, ())
        self.assertEqual([skip.row_id for skip in report.skipped], ['-'])
        self.assertIn('本轮未登记任何归还', self.text(report))

    def test_a_row_that_cannot_be_read_is_a_visible_skip(self):
        self.stage()
        self.port.submit(read_error=Code.EVIDENCE)
        report = self.intake.run()
        self.assertEqual([skip.code for skip in report.skipped], [Code.EVIDENCE.value])
        self.assertEqual([skip.note for skip in report.skipped], [READ_NOTE])
        self.assertEqual(self.sources.pending(), ())

    def test_a_row_whose_sole_loan_cannot_be_found_says_what_to_do(self):
        self.stage()
        self.port.submit(resolve_error=Code.EVIDENCE)
        report = self.intake.run()
        self.assertEqual(report.findings, ())
        self.assertEqual([skip.code for skip in report.skipped], [Code.EVIDENCE.value])
        self.assertEqual([skip.note for skip in report.skipped],
                         [REGISTER_NOTES[Code.EVIDENCE.value]])
        self.assertIn('需在归还表单填「归还物品」指定单据', self.text(report))

    def test_the_instance_gate_stops_the_pass_instead_of_being_recorded(self):
        self.stage()
        self.port.submit(loan=self.borrowed.ref)
        self.locks.code = Code.INSTANCE
        with self.assertRaises(ContractError) as raised:
            self.intake.run()
        self.assertEqual(raised.exception.code, Code.INSTANCE)
        self.assertEqual(self.sources.pending(), ())


class WatermarkTests(ReturnIntakeHarness):
    def test_a_return_before_the_watermark_is_history_not_work(self):
        self.stage()
        self.port.submit('synthetic-old-row', occurred=HISTORIC, loan=self.borrowed.ref)
        report = self.intake.run()
        self.assertEqual(report.history, ('synthetic-old-row',))
        self.assertEqual(report.findings, ())
        text = self.text(report)
        self.assertIn('历史行（归还时间在水位之前，不登记）', text)
        self.assertIn('synthetic-old-row', text)

    def test_without_a_watermark_nothing_is_registered_and_the_report_says_so(self):
        self.stage(since=None)
        self.port.submit(loan=self.borrowed.ref)
        report = self.intake.run()
        self.assertEqual(report.since, '')
        self.assertEqual(report.scanned, 1)
        self.assertEqual(report.findings, ())
        self.assertEqual([skip.code for skip in report.skipped], [Code.CONFIG.value])
        self.assertEqual(self.sources.pending(), ())
        text = self.text(report)
        self.assertIn('水位未配置', text)
        self.assertIn('return_intake.since', text)

    def test_the_report_line_names_the_table_the_watermark_and_the_counts(self):
        self.stage()
        self.port.submit(loan=self.borrowed.ref)
        self.port.submit('synthetic-old-row', occurred=HISTORIC, loan=self.borrowed.ref)
        text = self.text(self.intake.run())
        self.assertIn('归还发现：扫描 2，本机已有 0，本轮登记 1，跳过 0', text)
        self.assertIn(f'container={ENTRY_CONTAINER}', text)
        self.assertIn('只处理归还时间 ≥', text)
        self.assertIn(f'登记 归还 row={RETURN_ROW}', text)

    def test_the_report_carries_only_row_ids_codes_and_static_notes(self):
        """脱敏：报告里出现人名或单号就是漏；行 id + 原因码 + 固定文案够人接手。"""
        self.stage()
        self.port.submit(loan=self.borrowed.ref)
        self.port.submit('synthetic-bad-row', read_error=Code.EVIDENCE)
        text = self.text(self.intake.run())
        self.assertIn(f'row={RETURN_ROW}', text)
        self.assertIn(f'row=synthetic-bad-row {Code.EVIDENCE.value}', text)
        self.assertIn(READ_NOTE, text)
        self.assertNotIn(BORROWER.user_id, text)
        self.assertNotIn(self.borrowed.ref.resource_id, text)


class ReportTests(unittest.TestCase):
    def test_the_two_discovery_lines_do_not_overwrite_each_other(self):
        applications = IntakeReport(scanned=4, known=3, findings=(), skipped=())
        returns = ReturnReport(scanned=1, known=0, history=('synthetic-old-row',))
        text = '\n'.join(format_drive_lines(
            DriveReport((), (), (), (), intake=applications, returns=returns)))
        self.assertIn('申请发现：扫描 4，本机已有 3，本轮登记 0，跳过 0', text)
        self.assertIn('归还发现：扫描 1，本机已有 0，本轮登记 0，跳过 0', text)

    def test_a_pass_without_return_discovery_keeps_the_old_report(self):
        text = '\n'.join(format_drive_lines(DriveReport((), (), (), ())))
        self.assertNotIn('归还发现', text)

    def test_the_exit_code_flags_a_return_scan_without_a_conclusion(self):
        self.assertEqual(drive_exit_code(DriveReport((), (), (), ())), 0)
        self.assertEqual(drive_exit_code(
            DriveReport((), (), (), (), returns=ReturnReport(scanned=0))), 0)
        # 不配水位的 dry-run 不是失败：逐行 CONFIG 记在发现账目里，scan_code 为空。
        self.assertEqual(drive_exit_code(
            DriveReport((), (), (), (), returns=ReturnReport(
                scanned=1, skipped=(ReturnSkip('row', Code.CONFIG.value, ''),)))), 0)
        self.assertEqual(drive_exit_code(
            DriveReport((), (), (), (), returns=ReturnReport(
                scan_code=Code.UNKNOWN.value))), 1)

    def test_a_failed_return_scan_still_leaves_the_pass_running(self):
        """发现坏了只是账目里的一条：报告照出，摘要不崩。"""
        report = ReturnReport(scan_code=Code.UNKNOWN.value,
                              skipped=(ReturnSkip('-', Code.UNKNOWN.value, ''),))
        text = '\n'.join(format_drive_lines(DriveReport((), (), (), (), returns=report)))
        self.assertIn('归还发现：扫描 0', text)
        self.assertIn(f'跳过 归还 row=- {Code.UNKNOWN.value}', text)


class WiringTests(ReturnIntakeHarness):
    def test_the_intake_is_only_wired_when_the_port_can_scan(self):
        engine = FakeEngine(runtime_binding(), FakeReader((self.borrowed,)))
        self.assertIsNone(return_intake(engine, object(), self.sources, FakeStore(),
                                        self.locks, {}))
        port = FakeReturnPort()
        self.assertIsNotNone(return_intake(engine, port, self.sources, FakeStore(),
                                           self.locks, {}))
        legacy = FakeReturnPort(container='')
        self.assertIsNone(return_intake(engine, legacy, self.sources, FakeStore(),
                                        self.locks, {}))

    def test_the_watermark_comes_from_the_binding_document(self):
        engine = FakeEngine(runtime_binding(), FakeReader((self.borrowed,)))
        port = FakeReturnPort()
        wired = return_intake(engine, port, self.sources, FakeStore(), self.locks,
                              {'return_intake': {'since': WATERMARK.isoformat()}})
        self.assertEqual(wired.since, WATERMARK)
        unwired = return_intake(engine, port, self.sources, FakeStore(), self.locks, {})
        self.assertIsNone(unwired.since)

    def test_a_broken_watermark_is_config_not_absent(self):
        engine = FakeEngine(runtime_binding(), FakeReader((self.borrowed,)))
        port = FakeReturnPort()
        with self.assertRaises(ContractError) as raised:
            return_intake(engine, port, self.sources, FakeStore(), self.locks,
                          {'return_intake': {'since': '2029-12-30T08:00:00'}})
        self.assertEqual(raised.exception.code, Code.CONFIG)
