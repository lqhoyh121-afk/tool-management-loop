"""L1/L2 lending drive over FileJournal. SYNTHETIC ports only; no live DingTalk."""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t04_binding_doc import binding_document

from bootstrap.binding import save_binding
from bootstrap.drive import (HANG_NOTES, DriveLoop, FileSources, Hang, StaticSources,
                             WorkItem, drive_exit_code, dump_sources, format_drive_lines,
                             run_bound_drive)
from bootstrap.instance import MachineLock, pid_alive, slot_path
from bootstrap.journal import FileJournal
from bootstrap.wizard import main
from contracts.model import Action, Code, ContractError, Identity, IdentityBinding, Inventory, Resource, State
from contracts.ports import (LedgerScope, RuntimeBinding, StageReceipt, lease_key,
                             stage_operation_id)
from contracts.flow import Outcome, Receipt
from workflow.engine import LendingEngine


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / 'contracts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


synthetic = _load('t10_drive_synthetic', 'synthetic.py')
fixtures = _load('t10_drive_fixtures', 'fixtures.py')

RETURN = Resource('record', 'synthetic-org', 'synthetic-returns', 'synthetic-return-1')
ISSUE_TASK = Resource('todo', 'synthetic-org', 'synthetic-todos', 'synthetic-issue-todo')
RETURN_TASK = Resource('todo', 'synthetic-org', 'synthetic-todos', 'synthetic-return-todo')
INTERNAL_MANAGER = Identity('todo', 'synthetic-org', 'synthetic-internal-manager')
ENV = {
    'system_name': 'Windows',
    'version_info': (3, 11, 4, 'final', 0),
    'tkinter_available': True,
}


class MirrorReader:
    def __init__(self, writer):
        self.writer = writer
        self.events = {}
        self.seed_loans = {}

    def set_loan(self, loan):
        self.seed_loans[loan.ref.resource_id] = loan
        self.writer.register(loan)

    def set_event(self, loan_ref, source, event):
        self.events[(loan_ref, source)] = event

    def read_loan(self, ref):
        return self.writer.loan_states.get(ref.resource_id, self.seed_loans[ref.resource_id])

    def read_inventory(self, ref):
        stock = self.writer.ledger_stock
        if stock is None or stock.ref != ref:
            raise ContractError(Code.WRONG_LOAN)
        return stock

    def read_event(self, loan, source):
        return self.events[(loan.ref, source)]


class SyntheticStages:
    def __init__(self):
        self.created = {}

    def create_stage(self, request, binding, lease):
        if request.action in (Action.ISSUE, Action.RETURN):
            source = ISSUE_TASK if request.action == Action.ISSUE else RETURN_TASK
            identity_binding = IdentityBinding(request.actor, INTERNAL_MANAGER, source,
                                               'synthetic-stage-create',
                                               'synthetic-stage-readback')
        else:
            source = fixtures.FORM
            identity_binding = None
        receipt = StageReceipt(request.operation_id, Outcome.VERIFIED, source,
                               identity_binding, 'synthetic-stage-create',
                               'synthetic-stage-readback')
        self.created[request.operation_id] = receipt
        return receipt

    def query_stage(self, request):
        return self.created.get(
            request.operation_id, StageReceipt(request.operation_id, Outcome.UNKNOWN))


class SharedLedgerWriter(synthetic.SyntheticWriter):
    def __init__(self, journal, leases):
        super().__init__(None, None, journal, leases)
        self.loan_states = {}
        self.ledger_stock = None

    def register(self, loan):
        self.loan_states[loan.ref.resource_id] = loan

    def submit(self, intent, binding, lease):
        self.current = self.loan_states.get(intent.before.ref.resource_id)
        self.inventory = self.ledger_stock
        receipt = super().submit(intent, binding, lease)
        self.loan_states[intent.after.ref.resource_id] = self.current
        self.ledger_stock = self.inventory
        return receipt


class DriveHarness:
    def __init__(self, runtime, lock_root, journal=None):
        self.runtime = Path(runtime)
        self.lock_root = Path(lock_root)
        self.journal = journal or FileJournal(self.runtime / 'operations')
        self.locks = MachineLock(self.lock_root)
        self.writer = SharedLedgerWriter(self.journal, self.locks)
        self.reader = MirrorReader(self.writer)
        self.stages = SyntheticStages()
        self.binding = RuntimeBinding(
            fixtures.MANAGER, LedgerScope.from_record(fixtures.ITEM),
            'synthetic-config-v1', fixtures.MANAGER, fixtures.MANAGER,
            'synthetic-binding-readback', True, True, True, True)
        stock = Inventory(fixtures.ITEM, 5, 0, 0, (), (), (), 'synthetic-rev-1')
        loan = fixtures.loan()
        self.reader.set_loan(loan)
        self.writer.ledger_stock = stock
        self.engine = LendingEngine(self.reader, self.writer, self.stages,
                                    self.journal, self.locks, self.binding)
        self.engine.start(self.binding.ledger, self.binding.account)

    def stop(self):
        if self.engine.lease is not None:
            self.engine.stop()

    def event(self, action, actor=None, **changes):
        base = fixtures.event(action, actor or fixtures.MANAGER)
        base = replace(base, occurred_at=fixtures.NOW - timedelta(hours=1))
        return replace(base, **changes) if changes else base

    def todo_event(self, action, task):
        binding = IdentityBinding(fixtures.MANAGER, INTERNAL_MANAGER, task,
                                  'synthetic-create', 'synthetic-get')
        return replace(self.event(action), actor=INTERNAL_MANAGER, source=task,
                       evidence_kind='todo_completion', binding=binding)


class DriveTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.runtime = self.root / 'runtime'
        self.locks = self.root / 'locks'
        save_binding(self.runtime, binding_document())

    def tearDown(self):
        self._temp.cleanup()

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_full_loop_through_drive_and_file_journal(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            approve = harness.event(Action.APPROVE)
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
            first = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()
            self.assertTrue(first.processed)
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_ISSUE)
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 2, 0),
            )

            issue = harness.todo_event(Action.ISSUE, ISSUE_TASK)
            harness.reader.set_event(fixtures.LOAN, ISSUE_TASK, issue)
            second = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, ISSUE_TASK),)),
                harness.journal, harness.locks).run()
            self.assertTrue(second.processed)
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.BORROWED)

            request = harness.event(Action.REQUEST_RETURN, actor=fixtures.BORROWER,
                                    return_ref=RETURN, quantity=2, physical_ids=())
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, request)
            third = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_RETURN)

            returned = harness.todo_event(Action.RETURN, RETURN_TASK)
            harness.reader.set_event(fixtures.LOAN, RETURN_TASK, returned)
            fourth = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, RETURN_TASK),)),
                harness.journal, harness.locks).run()
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.CLOSED)
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (5, 0, 0),
            )
            self.assertEqual(harness.writer.writes, 5)
        finally:
            harness.stop()

    def test_every_processed_item_is_named_with_its_transition(self):
        """摘要里的「处理 N」必须条条点得出名字与迁移（#93）。

        真机实测：一条已完成的阶段待办被消费、台账推到已借出，报告里除「处理 1」之外
        一个字都没有 —— 「漏消费」与「消费了」在日志上长得一模一样。所以每条处理都要有
        一行，且行里的迁移是这一条的**净效果**（审批这一条落了两笔写，只报一段）。
        """
        harness = DriveHarness(self.runtime, self.locks)
        try:
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM,
                                     harness.event(Action.APPROVE))
            sources = StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),))

            report = DriveLoop(harness.engine, sources, harness.journal,
                               harness.locks).run()

            self.assertEqual(len(report.processed), 1)
            self.assertEqual(len(report.evidence), 1)
            named = report.evidence[0]
            self.assertEqual((named.kind, named.loan_id, named.source_kind, named.source_id),
                             ('event', fixtures.LOAN.resource_id,
                              fixtures.FORM.kind, fixtures.FORM.resource_id))
            self.assertEqual(named.before_state, State.AWAITING_APPROVAL.value)
            self.assertEqual(named.after_state, State.AWAITING_ISSUE.value)
            lines = format_drive_lines(report)
            self.assertEqual(sum(1 for line in lines if line.startswith('  处理 ')),
                             len(report.processed))
            self.assertIn(f'  处理 event loan={fixtures.LOAN.resource_id} '
                          f'form={fixtures.FORM.resource_id} '
                          f'awaiting_approval→awaiting_issue_confirmation',
                          '\n'.join(lines))
        finally:
            harness.stop()

    def test_a_pass_that_did_nothing_names_nothing(self):
        """反向判据：没得处理时不许印「处理」行（有行就等于说推进了单）。"""
        harness = DriveHarness(self.runtime, self.locks)
        try:
            report = DriveLoop(harness.engine, StaticSources(()), harness.journal,
                               harness.locks).run()
            self.assertEqual((report.processed, report.evidence), ((), ()))
            lines = format_drive_lines(report)
            self.assertIn('处理 0，', lines[0])
            self.assertNotIn('  处理 ', '\n'.join(lines))
        finally:
            harness.stop()

    def test_apply_source_creates_approval_stage(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            apply_event = harness.event(Action.APPLY, actor=fixtures.BORROWER,
                                        quantity=2, physical_ids=())
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, apply_event)
            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('apply', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()
            self.assertTrue(report.processed)
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_APPROVAL)
            self.assertEqual(len(harness.stages.created), 1)
        finally:
            harness.stop()

    def test_reconcile_skips_unbindable_loan_without_aborting_pass(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            stale = replace(fixtures.loan(), config_version='stale-config')
            harness.reader.set_loan(stale)
            harness.writer.register(stale)
            sources = StaticSources((WorkItem('event', stale.ref, fixtures.FORM),))

            report = DriveLoop(harness.engine, sources, harness.journal, harness.locks).run()

            stage_skips = [o for o in report.skipped if o.kind == 'stage']
            self.assertEqual(len(stage_skips), 1)
            self.assertEqual(stage_skips[0].code, Code.CONFIG.value)
            self.assertEqual(harness.stages.created, {})
        finally:
            harness.stop()

    def test_reconcile_creates_missing_return_stage_without_driving_event(self):
        from contracts.ports import stage_operation_id

        harness = DriveHarness(self.runtime, self.locks)
        try:
            borrowed = replace(fixtures.loan(), state=State.BORROWED)
            harness.reader.set_loan(borrowed)
            harness.writer.register(borrowed)
            # 借出已完成，但这一轮没有该单的有效事件：入口只能靠对账补齐。
            # （真实现象：借出发生在驱动之外或多轮之前，归还入口行不存在。）
            harness.reader.set_event(borrowed.ref, fixtures.FORM,
                                     harness.todo_event(Action.ISSUE, ISSUE_TASK))
            sources = StaticSources((WorkItem('event', borrowed.ref, fixtures.FORM),))
            expected = stage_operation_id(borrowed, Action.REQUEST_RETURN)

            DriveLoop(harness.engine, sources, harness.journal, harness.locks).run()
            self.assertIn(expected, harness.stages.created)

            before = set(harness.stages.created)
            DriveLoop(harness.engine, sources, harness.journal, harness.locks).run()
            self.assertEqual(set(harness.stages.created) - before, set())
        finally:
            harness.stop()

    def test_stage_reconcile_is_reported_apart_from_recovery(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            borrowed = replace(fixtures.loan(), state=State.BORROWED)
            harness.reader.set_loan(borrowed)
            harness.writer.register(borrowed)
            harness.reader.set_event(borrowed.ref, fixtures.FORM,
                                     harness.todo_event(Action.ISSUE, ISSUE_TASK))
            sources = StaticSources((WorkItem('event', borrowed.ref, fixtures.FORM),))

            report = DriveLoop(harness.engine, sources, harness.journal, harness.locks).run()
            expected = stage_operation_id(borrowed, Action.REQUEST_RETURN)

            # 新建入口是这一轮真实的外写，不是回查：回查数里不能出现它。
            self.assertEqual(report.recovered, ())
            self.assertNotIn(expected, report.recovered)
            self.assertEqual(report.stage_checked, (borrowed.ref,))
            self.assertEqual(report.stage_created, (expected,))
            self.assertIn(expected, harness.stages.created)

            lines = format_drive_lines(report)
            self.assertIn('回查 0，', lines[0])
            self.assertNotIn('含阶段对账新建', '\n'.join(lines))
            self.assertEqual(lines[1], '阶段对账：检查 1，新建 1，跳过 0')
        finally:
            harness.stop()

    def test_reconcile_reports_unreadable_loan_as_a_visible_skip(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            loan = fixtures.loan()
            harness.reader.set_loan(loan)
            harness.writer.register(loan)
            harness.reader.set_event(loan.ref, fixtures.FORM, harness.event(Action.APPROVE))

            def unreadable(_ref):
                raise ContractError(Code.EVIDENCE)

            harness.reader.read_loan = unreadable
            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', loan.ref, fixtures.FORM),)),
                harness.journal, harness.locks).run()

            stage_skips = [o for o in report.skipped if o.kind == 'stage']
            self.assertEqual(len(stage_skips), 1)
            self.assertEqual(stage_skips[0].code, Code.EVIDENCE.value)
            self.assertEqual(stage_skips[0].loan_id, loan.ref.resource_id)
            lines = '\n'.join(format_drive_lines(report))
            self.assertIn(f'跳过 stage loan={loan.ref.resource_id} '
                          f'stage={loan.ref.resource_id} EVIDENCE_REQUIRED', lines)
            self.assertIn('阶段对账：检查 1，新建 0，跳过 1', lines)
            self.assertEqual(harness.stages.created, {})
        finally:
            harness.stop()

    def test_reconcile_reports_a_state_without_a_next_human_stage(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            closed = replace(fixtures.loan(), state=State.CLOSED)
            harness.reader.set_loan(closed)
            harness.writer.register(closed)
            harness.reader.set_event(closed.ref, fixtures.FORM, harness.event(Action.APPROVE))

            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', closed.ref, fixtures.FORM),)),
                harness.journal, harness.locks).run()

            stage_skips = [o for o in report.skipped if o.kind == 'stage']
            self.assertEqual(len(stage_skips), 1)
            self.assertEqual(stage_skips[0].code, Code.STATE.value)
            lines = '\n'.join(format_drive_lines(report))
            self.assertIn(f'跳过 stage loan={closed.ref.resource_id} '
                          f'stage={closed.ref.resource_id} INVALID_STATE', lines)
            self.assertIn('阶段对账：检查 1，新建 0，跳过 1', lines)
            self.assertEqual(harness.stages.created, {})
        finally:
            harness.stop()

    def test_reconcile_covers_a_loan_known_only_from_a_journal_intent(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            issued = replace(fixtures.loan(), state=State.AWAITING_ISSUE)
            harness.reader.set_loan(issued)
            harness.writer.register(issued)
            harness.writer.ledger_stock = Inventory(fixtures.ITEM, 3, 2, 0, (), (), (),
                                                    'synthetic-rev-1')
            harness.reader.set_event(issued.ref, ISSUE_TASK,
                                     harness.todo_event(Action.ISSUE, ISSUE_TASK))
            # 对账新建的入口会被本轮当作一个来源行再读一次；这里放一条读不出下一步的
            # 历史待办完成（旧借出确认），让该行只报跳过、不再写台账。
            harness.reader.set_event(issued.ref, fixtures.FORM,
                                     harness.todo_event(Action.ISSUE, ISSUE_TASK))
            empty = StaticSources(())

            # 队列里没有该单、日志里也没有写入意图：对账无从知晓它，什么都不建。
            before = DriveLoop(harness.engine, empty, harness.journal, harness.locks).run()
            self.assertEqual(before.stage_checked, ())
            self.assertEqual(harness.stages.created, {})

            # 本机索引里没有它的记录，只剩日志里先前那次写入意图。
            harness.engine.execute(issued.ref, ISSUE_TASK)

            report = DriveLoop(harness.engine, empty, harness.journal, harness.locks).run()
            expected = stage_operation_id(harness.reader.read_loan(issued.ref),
                                          Action.REQUEST_RETURN)
            self.assertEqual(report.recovered, ())
            self.assertEqual(report.stage_checked, (issued.ref,))
            self.assertEqual(report.stage_created, (expected,))
            self.assertIn(expected, harness.stages.created)
            # 对账只补人工入口，不重发台账写入。
            self.assertEqual(harness.writer.writes, 1)
        finally:
            harness.stop()

    def test_unknown_result_queries_original_intent_on_restart(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            approve = harness.event(Action.APPROVE)
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
            harness.writer.lose_response = True
            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()
            self.assertEqual(len(report.blocked), 1)
            self.assertEqual(report.blocked[0].code, Code.UNKNOWN.value)
            self.assertEqual(harness.writer.writes, 1)
            op_id = harness.journal.unresolved_ids()[0]
            harness.stop()
            harness.writer.lose_response = False
            restarted = DriveHarness(self.runtime, self.locks, journal=FileJournal(
                self.runtime / 'operations'))
            restarted.writer.loan_states = harness.writer.loan_states
            restarted.writer.ledger_stock = harness.writer.ledger_stock
            restarted.writer.current = harness.writer.current
            restarted.writer.inventory = harness.writer.inventory
            restarted.writer.writes = harness.writer.writes
            restarted.reader.events = harness.reader.events
            restarted.reader.seed_loans = harness.reader.seed_loans
            try:
                recovered = DriveLoop(
                    restarted.engine, StaticSources(()), restarted.journal, restarted.locks).run()
                self.assertEqual(recovered.recovered, (op_id,))
                # 重启后除了回查未决意图，还把这笔「审批已写、预留未写」的单补完：
                # 预留随之上账，状态推进到待领用确认（旧行为是永远停在 reservation_pending）。
                self.assertEqual(restarted.reader.read_loan(fixtures.LOAN).state,
                                 State.AWAITING_ISSUE)
                # 第一次写入是审批意图，第二次是补齐被中断的预留 —— 都是真实步骤，不是盲目重发。
                self.assertEqual(restarted.writer.writes, 2)
            finally:
                restarted.stop()
        finally:
            if harness.engine.lease is not None:
                harness.stop()

    def approval_written(self, harness):
        """Stop between the two external writes: 审批已写、预留未写."""
        harness.engine.ensure_stage(harness.reader.read_loan(fixtures.LOAN), Action.APPROVE)
        approve = harness.event(Action.APPROVE)
        harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
        execution = harness.engine.execute(fixtures.LOAN, fixtures.FORM)
        self.assertEqual(execution.outcome, Outcome.VERIFIED)
        self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state,
                         State.RESERVATION_PENDING)
        return approve

    def test_pending_reservation_finishes_reserve_on_next_pass(self):
        """审批已写、预留未写: the drive finishes the reservation, once."""
        harness = DriveHarness(self.runtime, self.locks)
        try:
            self.approval_written(harness)
            first = DriveLoop(harness.engine, StaticSources(()), harness.journal,
                              harness.locks).run()
            # 无下一阶段现在作为可见跳过出现（PR #70），不影响自愈。
            self.assertEqual([(o.kind, o.code) for o in first.skipped],
                             [('stage', Code.STATE.value)])
            self.assertEqual(len(first.processed), 1)
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_ISSUE)
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 2, 0),
            )
            self.assertEqual(harness.writer.writes, 2)

            issue = harness.todo_event(Action.ISSUE, ISSUE_TASK)
            harness.reader.set_event(fixtures.LOAN, ISSUE_TASK, issue)
            second = DriveLoop(harness.engine, StaticSources(()), harness.journal,
                               harness.locks).run()
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.BORROWED)
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 0, 2),
            )
            self.assertEqual(harness.writer.writes, 3)
            self.assertEqual([outcome.code for outcome in second.skipped],
                             [Code.DUPLICATE.value])
        finally:
            harness.stop()

    def test_unprovable_pending_reservation_is_a_visible_blocked_item(self):
        """No readable approval write on this machine: report it, never guess."""
        harness = DriveHarness(self.runtime, self.locks)
        try:
            approve = harness.event(Action.APPROVE)
            loan, stock = fixtures.settled(fixtures.loan(), approve, fixtures.stock())
            harness.reader.set_loan(loan)
            harness.writer.ledger_stock = stock
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
            report = DriveLoop(
                harness.engine,
                StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()
            self.assertEqual(report.processed, ())
            # 无下一阶段现在作为可见跳过出现（PR #70），不影响自愈。
            self.assertEqual([(o.kind, o.code) for o in report.skipped],
                             [('stage', Code.STATE.value)])
            self.assertEqual(len(report.blocked), 1)
            # 挂起码自描述：报告里直接读得出「翻不到审批证据」，不是笼统的 INVALID_STATE。
            # 这里钉的是台账/日志里实际出现的字面码，不是枚举成员本身。
            self.assertEqual(report.blocked[0].code, 'HANG_APPROVAL_UNPROVEN')
            self.assertNotIn(Code.STATE.value, [o.code for o in report.blocked])
            self.assertEqual(report.blocked[0].note, HANG_NOTES[Hang.APPROVAL_UNPROVEN])
            lines = '\n'.join(format_drive_lines(report))
            self.assertIn(
                f'挂起 event loan=synthetic-loan form=synthetic-form '
                f'{Hang.APPROVAL_UNPROVEN.value} 审批已写、预留未写',
                lines,
            )
            # 摘要一眼分得开：挂起是挂起，「待人工」里点名它并带上为什么挂。
            self.assertIn(f'挂起 1（{Hang.APPROVAL_UNPROVEN.value}×1）', lines)
            self.assertIn('待人工 1 条', lines)
            self.assertIn('要人查/补', lines)
            self.assertEqual(drive_exit_code(report), 1)
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state,
                             State.RESERVATION_PENDING)
            self.assertEqual(harness.writer.ledger_stock.available, 5)
            self.assertEqual(harness.writer.ledger_stock.reserved, 0)
            self.assertEqual(harness.writer.writes, 0)
        finally:
            harness.stop()

    def test_skip_reports_error_code(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            approve = harness.event(Action.APPROVE)
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
            original = harness.reader.read_event

            def read_event(loan, source):
                if source == ISSUE_TASK:
                    raise ContractError(Code.EVIDENCE)
                return original(loan, source)

            harness.reader.read_event = read_event
            report = DriveLoop(
                harness.engine,
                StaticSources((
                    WorkItem('event', fixtures.LOAN, fixtures.FORM),
                    WorkItem('event', fixtures.LOAN, ISSUE_TASK),
                )),
                harness.journal, harness.locks).run()
            self.assertEqual(len(report.processed), 1)
            self.assertEqual(len(report.skipped), 1)
            self.assertEqual(report.skipped[0].code, Code.EVIDENCE.value)
            summary = format_drive_lines(report)[0]
            self.assertIn('EVIDENCE_REQUIRED×1', summary)
            self.assertIn('跳过 event loan=synthetic-loan todo=synthetic-issue-todo',
                            '\n'.join(format_drive_lines(report)))
        finally:
            harness.stop()

    def test_reject_path_does_not_reserve(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            reject = harness.event(Action.REJECT)
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, reject)
            DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.REJECTED)
            self.assertEqual(harness.writer.ledger_stock.available, 5)
            self.assertEqual(harness.writer.ledger_stock.reserved, 0)
        finally:
            harness.stop()

    def test_second_instance_blocked(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            other = MachineLock(self.locks)
            self.blocked(Code.INSTANCE, lambda: other.acquire(
                harness.binding.ledger, harness.binding.account))
        finally:
            harness.stop()

    def test_lost_lease_fails_closed(self):
        harness = DriveHarness(self.runtime, self.locks)
        try:
            harness.locks.release(harness.engine.lease)
            harness.engine.lease = 'missing'
            self.blocked(Code.INSTANCE, lambda: DriveLoop(
                harness.engine, StaticSources(()), harness.journal, harness.locks).run())
        finally:
            harness.engine.lease = None

    def test_summary_separates_waiting_on_human_from_missing_evidence(self):
        """摘要要能一眼分清：正常等人（阶段待办没完成）vs 挂起（证据不足/未决写）。"""
        from bootstrap.drive import DriveOutcome, DriveReport, format_drive_lines

        waiting = DriveOutcome('event', 'recLoan', 'todo', 'task-1', Code.STATE.value)
        broken = DriveOutcome('event', 'recLoan', 'form', 'row-1', 'EVIDENCE_REQUIRED')
        hang = DriveOutcome('event', 'recLoan', 'form', 'row-2', Hang.APPROVAL_UNPROVEN.value,
                            HANG_NOTES[Hang.APPROVAL_UNPROVEN])
        lines = '\n'.join(format_drive_lines(DriveReport((), (), (waiting, broken), (hang,))))

        # 挂起进「待人工」总数，但与阶段待办分开列：一个是等人点，一个是要人查/补。
        self.assertIn('待人工 2 条', lines)
        self.assertIn('阶段待办未完成 1 条（task-1）', lines)
        self.assertIn(f'挂起 1 条（要人查/补', lines)
        self.assertIn(f'{Hang.APPROVAL_UNPROVEN.value}×1（审批已写、预留未写', lines)
        self.assertIn('task-1', lines)
        self.assertIn('EVIDENCE_REQUIRED×1', lines)
        self.assertIn(f'{Code.STATE.value}×1', lines)
        self.assertIn('row-2', lines)

    def test_exit_code_flags_hangs_and_real_failures_but_not_steady_state(self):
        """退出码回答「要不要人看」：稳态（等人点待办 / 早就消费过）不当失败。"""
        from bootstrap.drive import DriveOutcome, DriveReport, drive_exit_code
        from bootstrap.inbox import IntakeReport

        def report(skipped=(), blocked=(), intake=None):
            return DriveReport((), (), tuple(skipped), tuple(blocked), intake=intake)

        def outcome(kind, source_kind, code):
            return DriveOutcome(kind, 'recLoan', source_kind, 'src-1', code)

        self.assertEqual(drive_exit_code(report()), 0)
        self.assertEqual(drive_exit_code(
            report(skipped=(outcome('event', 'todo', Code.STATE.value),))), 0)
        self.assertEqual(drive_exit_code(
            report(skipped=(outcome('event', 'form', Code.DUPLICATE.value),))), 0)
        # 非稳态的跳过与挂起都要人看。
        self.assertEqual(drive_exit_code(
            report(skipped=(outcome('event', 'form', Code.EVIDENCE.value),))), 1)
        self.assertEqual(drive_exit_code(
            report(skipped=(outcome('event', 'form', Code.READBACK.value),))), 1)
        self.assertEqual(drive_exit_code(
            report(blocked=(outcome('recover', 'form', Hang.UNRESOLVED_WRITE.value),))), 1)
        # 发现扫描本身没结论：缺表不等于空表。
        self.assertEqual(drive_exit_code(report(intake=IntakeReport(scan_code=Code.UNKNOWN.value))), 1)
        # 发现只出报告（dry-run）不是失败：逐行 CONFIG 记在发现账目里，scan_code 为空。
        self.assertEqual(drive_exit_code(report(intake=IntakeReport(scanned=0))), 0)

    def test_cli_drive_returns_non_zero_when_the_pass_hangs(self):
        """CLI 退出码要反映挂起：旧行为恒 0，调度层感知不到半写的单。"""
        harness = DriveHarness(self.runtime, self.locks)
        harness.stop()
        approve = harness.event(Action.APPROVE)
        loan, stock = fixtures.settled(fixtures.loan(), approve, fixtures.stock())
        harness.reader.set_loan(loan)
        harness.writer.ledger_stock = stock
        harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
        dump_sources(self.runtime / 'sources.json', (
            WorkItem('event', fixtures.LOAN, fixtures.FORM),
        ))
        stdout = io.StringIO()
        code = main(
            ['--drive', '--runtime', str(self.runtime), '--lock-root', str(self.locks)],
            stdin=io.StringIO(''),
            stdout=stdout,
            wait_on_error=False,
            environ_kwargs=dict(ENV, runtime_dir=str(self.runtime)),
            ports={
                'reader': harness.reader,
                'writer': harness.writer,
                'stages': harness.stages,
                'sources': FileSources(self.runtime / 'sources.json'),
                'locks': harness.locks,
                'store': harness.journal,
            },
        )
        text = stdout.getvalue()
        self.assertEqual(code, 1)
        self.assertIn(f'挂起 1（{Hang.APPROVAL_UNPROVEN.value}×1）', text)
        self.assertIn('待人工 1 条', text)
        self.assertIn('退出码 1', text)
        # 退出码只是汇总：这一轮的处置没变，仍然一张单都没动。
        self.assertEqual(harness.writer.writes, 0)
        self.assertEqual(harness.writer.ledger_stock.available, 5)

    def test_missing_binding_fails_closed(self):
        empty = self.root / 'empty-runtime'
        empty.mkdir()
        self.blocked(Code.CONFIG, lambda: run_bound_drive(empty, self.locks))

    def test_cli_drive_with_injected_ports(self):
        harness = DriveHarness(self.runtime, self.locks)
        harness.stop()
        approve = harness.event(Action.APPROVE)
        harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
        dump_sources(self.runtime / 'sources.json', (
            WorkItem('event', fixtures.LOAN, fixtures.FORM),
        ))
        stdout = io.StringIO()
        code = main(
            ['--drive', '--runtime', str(self.runtime), '--lock-root', str(self.locks)],
            stdin=io.StringIO(''),
            stdout=stdout,
            wait_on_error=False,
            environ_kwargs=dict(ENV, runtime_dir=str(self.runtime)),
            ports={
                'reader': harness.reader,
                'writer': harness.writer,
                'stages': harness.stages,
                'sources': FileSources(self.runtime / 'sources.json'),
                'locks': harness.locks,
                'store': harness.journal,
            },
        )
        self.assertEqual(code, 0)
        self.assertIn('驱动完成', stdout.getvalue())
        self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_ISSUE)

    def test_cli_drive_skips_the_round_when_another_instance_holds_the_lock(self):
        """被占用（不是死锁）：明说「上一轮未结束，本轮跳过」，这一轮一个写入都不做。

        退出码 0：占用中是「另一个实例正在推进」的正常稳态，跳过不是失败（等下一轮即可）。
        文字里保留 ``SECOND_INSTANCE_BLOCKED``，定时脚本现有的分支照着它认。
        """
        harness = DriveHarness(self.runtime, self.locks)      # 另一个实例：持锁在跑
        approve = harness.event(Action.APPROVE)
        harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
        dump_sources(self.runtime / 'sources.json', (
            WorkItem('event', fixtures.LOAN, fixtures.FORM),
        ))
        stdout = io.StringIO()
        code = main(
            ['--drive', '--runtime', str(self.runtime), '--lock-root', str(self.locks)],
            stdin=io.StringIO(''), stdout=stdout, wait_on_error=False,
            environ_kwargs=dict(ENV, runtime_dir=str(self.runtime)),
            ports={
                'reader': harness.reader,
                'writer': harness.writer,
                'stages': harness.stages,
                'sources': FileSources(self.runtime / 'sources.json'),
                'locks': harness.locks,
                'store': harness.journal,
            },
        )
        text = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn('上一轮未结束，本轮跳过', text)
        self.assertIn('SECOND_INSTANCE_BLOCKED', text)
        self.assertNotIn('锁接管', text)
        self.assertNotIn('驱动完成', text)
        self.assertEqual(harness.writer.writes, 0)
        self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_APPROVAL)
        harness.stop()

    def test_cli_drive_takes_over_an_abandoned_slot_and_says_so(self):
        """被杀进程留下的锁槽：这一轮自动接管并继续跑，报告与「被占用」分开说。"""
        harness = DriveHarness(self.runtime, self.locks)
        scope = harness.binding.ledger
        harness.stop()
        dead = next(candidate for candidate in range(999_999, 999_000, -1)
                    if pid_alive(candidate) is False)
        slot = slot_path(self.locks, scope)
        slot.mkdir(parents=True)
        (slot / 'scope').write_text(lease_key(scope) + '\n', encoding='utf-8')
        now = datetime.now().astimezone().isoformat(timespec='seconds')
        (slot / 'holder.json').write_text(json.dumps({
            'pid': dead, 'started_at': now, 'heartbeat_at': now}), encoding='utf-8')
        approve = harness.event(Action.APPROVE)
        harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
        dump_sources(self.runtime / 'sources.json', (
            WorkItem('event', fixtures.LOAN, fixtures.FORM),
        ))
        stdout = io.StringIO()
        code = main(
            ['--drive', '--runtime', str(self.runtime), '--lock-root', str(self.locks)],
            stdin=io.StringIO(''), stdout=stdout, wait_on_error=False,
            environ_kwargs=dict(ENV, runtime_dir=str(self.runtime)),
            ports={
                'reader': harness.reader,
                'writer': harness.writer,
                'stages': harness.stages,
                'sources': FileSources(self.runtime / 'sources.json'),
                'locks': harness.locks,
                'store': harness.journal,
            },
        )
        text = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn('锁接管', text)
        self.assertIn('不需要人工清槽', text)
        self.assertIn('驱动完成', text)
        self.assertNotIn('本轮跳过', text)
        # 接管之后这一轮真的跑完了业务：申请已批，进「待发放」。
        self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_ISSUE)
        self.assertFalse(slot.exists())

    def test_cli_drive_without_dws_config_fails_closed(self):
        stdout = io.StringIO()
        code = main(
            ['--drive', '--runtime', str(self.runtime), '--lock-root', str(self.locks)],
            stdin=io.StringIO(''),
            stdout=stdout,
            wait_on_error=False,
            environ_kwargs=dict(ENV, runtime_dir=str(self.runtime)),
        )
        self.assertEqual(code, 1)
        self.assertIn('闸门拒绝', stdout.getvalue())

    def test_live_adapter_missing_entry_fields_does_not_reuse_fields(self):
        from bootstrap.drive import live_adapter
        from bootstrap.journal import FileJournal
        data = binding_document(
            dws_cmd=[sys.executable, '-c', 'pass'],
            form_container='baseForm/tblForm',
            todo_container='todoSpace/executors',
        )
        del data['entry_fields']
        self.blocked(Code.CONFIG, lambda: live_adapter(
            self.runtime, FileJournal(self.runtime / 'operations'),
            MachineLock(self.locks), data))

    def test_live_adapter_missing_fields_does_not_use_synthetic(self):
        from bootstrap.drive import live_adapter
        from bootstrap.journal import FileJournal
        data = binding_document(
            dws_cmd=[sys.executable, '-c', 'pass'],
            form_container='baseForm/tblForm',
            todo_container='todoSpace/executors',
        )
        del data['fields']
        self.blocked(Code.CONFIG, lambda: live_adapter(
            self.runtime, FileJournal(self.runtime / 'operations'),
            MachineLock(self.locks), data))

    def test_title_display_is_absent_unless_the_binding_asks_for_it(self):
        from bootstrap.drive import live_adapter, title_display_from_document
        from bootstrap.journal import FileJournal
        data = binding_document(
            dws_cmd=[sys.executable, '-c', 'pass'],
            form_container='baseForm/tblForm',
            todo_container='todoSpace/executors',
        )
        self.assertIsNone(title_display_from_document(data))
        # 段在、但三个键都是空的：等于没配，标题保持老样子。
        self.assertIsNone(title_display_from_document(
            binding_document(title_display={'item_name_field': '',
                                            'borrower_names': False,
                                            'approve_entry_url': '  '})))
        adapter = live_adapter(
            self.runtime, FileJournal(self.runtime / 'operations'),
            MachineLock(self.locks), data)
        self.assertIsNone(adapter.title_display)

    def test_title_display_reads_the_optional_keys(self):
        from bootstrap.drive import title_display_from_document
        display = title_display_from_document(binding_document(title_display={
            'item_name_field': 'fldSYN-item-name',
            'borrower_names': True,
            'approve_entry_url': 'https://example.invalid/synthetic-entry',
        }))
        self.assertEqual(display.item_name_field, 'fldSYN-item-name')
        self.assertTrue(display.borrower_names)
        self.assertEqual(display.approve_entry_url,
                         'https://example.invalid/synthetic-entry')
        self.assertTrue(display.names_enabled)
        # 只给链接：仍然要拼链接，但不查名。
        link_only = title_display_from_document(binding_document(title_display={
            'approve_entry_url': 'https://example.invalid/synthetic-entry'}))
        self.assertFalse(link_only.names_enabled)
        self.assertTrue(link_only.approve_entry_url)

    def test_live_adapter_resolves_items_inside_the_ledger_scope(self):
        """#89：按「工具」名称解析物品的搜索范围 = 绑定台账作用域那一张表。

        解析出来的物品必须落在这个作用域里，否则建行时 ``check_binding`` 会按
        ``WRONG_LOAN`` 拦下 —— 所以范围与绑定同源，不引入第二个常量、也不猜。
        """
        from bootstrap.drive import live_adapter
        from bootstrap.journal import FileJournal

        document = binding_document(
            dws_cmd=[sys.executable, '-c', 'pass'],
            form_container='baseForm/tblForm',
            todo_container='todoSpace/executors',
        )
        adapter = live_adapter(
            self.runtime, FileJournal(self.runtime / 'operations'),
            MachineLock(self.locks), document)
        self.assertEqual(adapter.inventory_container,
                         document['ledger']['container_key'])
        # 调用方显式给值时以调用方的为准（注入替身的测试用得上）。
        injected = live_adapter(
            self.runtime, FileJournal(self.runtime / 'operations'),
            MachineLock(self.locks), document,
            inventory_container='synthetic-stock-injected')
        self.assertEqual(injected.inventory_container, 'synthetic-stock-injected')

    def test_broken_title_display_blocked_as_config(self):
        from bootstrap.drive import title_display_from_document
        for broken in ({'item_name_field': 7},
                       {'borrower_names': 'yes'},
                       {'approve_entry_url': ['https://example.invalid/x']},
                       'synthetic-not-a-block'):
            self.blocked(Code.CONFIG, lambda broken=broken: title_display_from_document(
                binding_document(title_display=broken)))


    def test_hand_edited_stock_snapshot_blocks_the_heal_without_moving_stock(self):
        """库存行已是「已预留」而借出行仍停在 reservation_pending：不许再迁一次库存。

        手改表造出的形状：审批已落库、预留也已经在库存行上，只有借出行没跟着走。
        这一跳的判据是审批回执记录的库存快照，不是「借出行还说 pending」。
        """
        harness = DriveHarness(self.runtime, self.locks)
        try:
            self.approval_written(harness)
            harness.writer.ledger_stock = Inventory(fixtures.ITEM, 3, 2, 0, (), (), (),
                                                    'synthetic-hand-edit')
            sources = StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),))

            report = DriveLoop(harness.engine, sources, harness.journal, harness.locks).run()

            self.assertEqual(report.processed, ())
            self.assertEqual([o.code for o in report.blocked], [Hang.STOCK_MOVED.value])
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state,
                             State.RESERVATION_PENDING)
            # 关键断言：库存一步没动（旧行为会再迁一次 → (1,4,0)）。
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 2, 0),
            )
            self.assertEqual(harness.writer.writes, 1)  # 只有那次审批写

            # 再跑一轮：顺序不变也不会自己动起来。
            again = DriveLoop(harness.engine, sources, harness.journal, harness.locks).run()
            self.assertEqual([o.code for o in again.blocked], [Hang.STOCK_MOVED.value])
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 2, 0),
            )
            self.assertEqual(harness.writer.writes, 1)
        finally:
            harness.stop()

    def test_approval_that_never_landed_blocks_the_heal(self):
        """审批回执不是 VERIFIED（NOT_SENT）：审批其实没写成功，不许照着它动库存。"""
        harness = DriveHarness(self.runtime, self.locks)
        try:
            approve = harness.event(Action.APPROVE)
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
            execution = harness.engine.execute(fixtures.LOAN, fixtures.FORM)
            self.assertEqual(execution.outcome, Outcome.VERIFIED)
            harness.reader.set_loan(replace(harness.reader.read_loan(fixtures.LOAN),
                                            state=State.RESERVATION_PENDING,
                                            consumed_events=(approve.event_id,)))
            # 平台其实没有收到这次审批写：回执改写成 NOT_SENT（带真实回读快照的形态，
            # 真适配器 query 就是这么回的），只有本机镜像以为它落地了。
            harness.journal.save_receipt(Receipt(
                execution.operation_id, Outcome.NOT_SENT, 'synthetic-not-sent-readback',
                harness.reader.read_loan(fixtures.LOAN), harness.writer.ledger_stock))

            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()

            self.assertEqual(report.processed, ())
            self.assertEqual([o.code for o in report.blocked], [Hang.APPROVAL_UNPROVEN.value])
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state,
                             State.RESERVATION_PENDING)
            # 关键断言：按未落地的审批发放是不允许的，库存不动。
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (5, 0, 0),
            )
            self.assertEqual(harness.writer.writes, 1)
        finally:
            harness.stop()

    def test_two_recorded_approvals_fail_closed(self):
        """两条都已落库、都被消费的审批：不挑一条用，挂起等人工，库存不动。

        挑任意一条都会让 operation_id 随候选而变，换来换去就不再幂等。
        """
        harness = DriveHarness(self.runtime, self.locks)
        try:
            first = harness.event(Action.APPROVE)
            second = replace(first, source=fixtures.APPLY_FORM, event_id='synthetic-approve-2')
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, first)
            harness.reader.set_event(fixtures.LOAN, fixtures.APPLY_FORM, second)
            harness.engine.execute(fixtures.LOAN, fixtures.FORM)
            # 第二个来源行也写了一次审批：借出行复位后重放一次，两条都真实落库。
            harness.reader.set_loan(replace(harness.reader.read_loan(fixtures.LOAN),
                                            state=State.AWAITING_APPROVAL))
            harness.engine.execute(fixtures.LOAN, fixtures.APPLY_FORM)

            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()

            self.assertEqual(report.processed, ())
            self.assertEqual([o.code for o in report.blocked], [Hang.APPROVAL_AMBIGUOUS.value])
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state,
                             State.RESERVATION_PENDING)
            # 关键断言：多候选必须拒绝，库存不动。
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (5, 0, 0),
            )
            self.assertEqual(harness.writer.writes, 2)
            # 挂起码自描述：多条候选与「一条都没翻到」不是同一件事，报告里分得开。
            self.assertIn(f'挂起 1（{Hang.APPROVAL_AMBIGUOUS.value}×1）',
                          '\n'.join(format_drive_lines(report)))
        finally:
            harness.stop()

    def test_blocked_sibling_entry_is_not_reported_as_an_unresolved_write(self):
        """同单另一条条目不得被牵连成「写入未决」：那是假话，还会把自愈饿死。

        审批回执是 verified，机器上没有任何未决写；同单第一条挂起不该让第二、第三条
        改口成 WRITE_UNKNOWN_QUERY_FIRST（旧口径会把三条同单条目数成「未决×2」）。
        """
        harness = DriveHarness(self.runtime, self.locks)
        try:
            approve = harness.event(Action.APPROVE)
            loan, stock = fixtures.settled(fixtures.loan(), approve, fixtures.stock())
            harness.reader.set_loan(loan)  # 借出行停在 reservation_pending、审批已被消费
            harness.writer.ledger_stock = stock
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, approve)
            harness.reader.set_event(fixtures.LOAN, fixtures.APPLY_FORM, approve)
            harness.reader.set_event(fixtures.LOAN, ISSUE_TASK, approve)

            report = DriveLoop(
                harness.engine,
                StaticSources((
                    WorkItem('event', fixtures.LOAN, fixtures.FORM),
                    WorkItem('event', fixtures.LOAN, fixtures.APPLY_FORM),
                    WorkItem('event', fixtures.LOAN, ISSUE_TASK),
                )),
                harness.journal, harness.locks).run()

            self.assertEqual([o.code for o in report.blocked],
                             [Hang.APPROVAL_UNPROVEN.value] * 3)
            self.assertNotIn(Code.UNKNOWN.value, [o.code for o in report.blocked])
            lines = '\n'.join(format_drive_lines(report))
            self.assertIn(f'挂起 3（{Hang.APPROVAL_UNPROVEN.value}×3）', lines)
            self.assertNotIn('WRITE_UNKNOWN_QUERY_FIRST', lines)
            self.assertIn('待人工 3 条', lines)
            self.assertEqual(drive_exit_code(report), 1)
            self.assertEqual(harness.writer.writes, 0)
            self.assertEqual(harness.writer.ledger_stock.available, 5)
        finally:
            harness.stop()

    def test_heal_does_not_depend_on_the_queue_row_it_arrived_on(self):
        """自愈的判据是日志里已落库的审批，不是队列那行的来源。

        同一份审批被复制成另一行时，那一行的条目也必须能补完预留；不然同单的
        坏行会把唯一能完成这一跳的条目一起挡住。
        """
        harness = DriveHarness(self.runtime, self.locks)
        try:
            self.approval_written(harness)
            approve = harness.reader.read_event(harness.reader.read_loan(fixtures.LOAN),
                                                fixtures.FORM)
            stale = fixtures.APPLY_FORM
            harness.reader.set_event(fixtures.LOAN, stale, approve)

            report = DriveLoop(
                harness.engine,
                StaticSources((
                    WorkItem('event', fixtures.LOAN, stale),
                    WorkItem('event', fixtures.LOAN, fixtures.FORM),
                )),
                harness.journal, harness.locks).run()

            self.assertEqual([o.code for o in report.blocked], [])
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_ISSUE)
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 2, 0),
            )
            self.assertEqual(harness.writer.writes, 2)
            self.assertEqual([o.code for o in report.healed], [Code.DUPLICATE.value])
        finally:
            harness.stop()

    def test_edited_decision_row_still_heals_from_the_recorded_approval(self):
        """审批行被改成拒绝：单不该只落一条跳过，预留按日志里的审批补上。"""
        harness = DriveHarness(self.runtime, self.locks)
        try:
            self.approval_written(harness)
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, harness.event(Action.REJECT))

            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()

            self.assertEqual(len(report.processed), 1)
            self.assertEqual(report.blocked, ())
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_ISSUE)
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 2, 0),
            )
            self.assertEqual(harness.writer.writes, 2)
            # 原错误码作为说明输出，不当作判据。
            self.assertEqual([o.code for o in report.healed], [Code.STATE.value])
            lines = '\n'.join(format_drive_lines(report))
            self.assertIn('自愈补齐 1（INVALID_STATE×1）', lines)
        finally:
            harness.stop()

    def test_unreadable_decision_row_still_heals_from_the_recorded_approval(self):
        """审批行读不出来（EVIDENCE_REQUIRED）：同样按已落库的审批补完预留。"""
        harness = DriveHarness(self.runtime, self.locks)
        try:
            self.approval_written(harness)

            def unreadable(_loan, _source):
                raise ContractError(Code.EVIDENCE)

            harness.reader.read_event = unreadable
            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()

            self.assertEqual(report.blocked, ())
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_ISSUE)
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 2, 0),
            )
            self.assertEqual(harness.writer.writes, 2)
            self.assertEqual([o.code for o in report.healed], [Code.EVIDENCE.value])
        finally:
            harness.stop()

    def test_reservation_reported_as_never_sent_is_a_visible_skip(self):
        """预留那一步平台回报 NOT_SENT：跳过它，不能静默计成「已处理」。"""
        harness = DriveHarness(self.runtime, self.locks)
        try:
            self.approval_written(harness)
            original = harness.writer.submit

            def submit(intent, binding, lease):
                if intent.event.action == Action.RESERVE:
                    return Receipt(intent.operation_id, Outcome.NOT_SENT)
                return original(intent, binding, lease)

            harness.writer.submit = submit
            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()

            self.assertEqual(report.processed, ())
            self.assertEqual([o.code for o in report.blocked], [])
            self.assertEqual([o.code for o in report.skipped if o.kind == 'event'],
                             [Code.READBACK.value])
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state,
                             State.RESERVATION_PENDING)
            self.assertEqual(harness.writer.ledger_stock.available, 5)
            self.assertEqual(harness.writer.ledger_stock.reserved, 0)
        finally:
            harness.stop()

    def test_an_unconsumed_approval_receipt_must_not_heal_the_reservation(self):
        """审批回执在日志里，但台账没有把它记成已消费：不得据此补预留。

        这条守卫是「审批事件必须在 consumed_events」那一句：没有它，一张回执就能
        让驱动在没有真实审批的情况下推着库存走。
        """
        harness = DriveHarness(self.runtime, self.locks)
        try:
            self.approval_written(harness)
            # 台账那本账没把这次审批记成已消费（手改/并写），本机镜像里也没有。
            harness.reader.set_loan(replace(harness.reader.read_loan(fixtures.LOAN),
                                           consumed_events=()))

            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()

            self.assertEqual(report.processed, ())
            self.assertEqual([o.code for o in report.blocked], [Hang.APPROVAL_UNPROVEN.value])
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state,
                             State.RESERVATION_PENDING)
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (5, 0, 0),
            )
            # 只有那次审批写；预留一次都没写。
            self.assertEqual(harness.writer.writes, 1)
            self.assertEqual(drive_exit_code(report), 1)
            lines = '\n'.join(format_drive_lines(report))
            self.assertIn(f'挂起 1（{Hang.APPROVAL_UNPROVEN.value}×1）', lines)
            self.assertIn('待人工 1 条', lines)
        finally:
            harness.stop()

    def test_another_loans_approval_cannot_heal_this_loan(self):
        """别人的审批回执（同一事件 id 被抄进本单）不得拿来补本单的预留。

        这是「来源匹配」那句守卫：候选审批必须以 **本单** 的台账行为来源。没有它，
        一张被抄了别人审批 id 的行就能推着本单库存走。
        """
        harness = DriveHarness(self.runtime, self.locks)
        try:
            other_ref = Resource('record', 'synthetic-org', 'synthetic-loans',
                                 'synthetic-other-loan')
            other = replace(fixtures.loan(), ref=other_ref)
            harness.reader.set_loan(other)
            harness.engine.ensure_stage(other, Action.APPROVE)
            other_approve = replace(harness.event(Action.APPROVE), loan_ref=other_ref)
            harness.reader.set_event(other_ref, fixtures.FORM, other_approve)
            self.assertEqual(harness.engine.execute(other_ref, fixtures.FORM).outcome,
                             Outcome.VERIFIED)
            self.assertEqual(harness.writer.writes, 1)

            # 本单停在待预留：台账行里被抄上了**别人的**审批事件 id。
            harness.reader.set_loan(replace(fixtures.loan(), state=State.RESERVATION_PENDING,
                                           consumed_events=(other_approve.event_id,)))
            harness.reader.set_event(fixtures.LOAN, fixtures.FORM, other_approve)

            report = DriveLoop(
                harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
                harness.journal, harness.locks).run()

            self.assertEqual([o.loan_id for o in report.blocked], ['synthetic-loan'])
            self.assertEqual([o.code for o in report.blocked], [Hang.APPROVAL_UNPROVEN.value])
            # 这一轮只补了别人那张单的预留（它自己的审批确实在日志里）。
            self.assertEqual([o.loan_ref for o in report.processed], [other_ref])
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 2, 0),
            )
            self.assertEqual(harness.writer.writes, 2)  # 别人的审批 + 别人的预留，就这两次
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state,
                             State.RESERVATION_PENDING)
        finally:
            harness.stop()

    def test_a_healed_loan_does_not_write_again_on_the_next_two_passes(self):
        """自愈不是每轮重写：补齐后连跑两轮，写入数与库存都不再变。"""
        harness = DriveHarness(self.runtime, self.locks)
        try:
            self.approval_written(harness)
            sources = StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),))
            first = DriveLoop(harness.engine, sources, harness.journal, harness.locks).run()
            self.assertEqual(harness.writer.writes, 2)
            self.assertEqual(first.blocked, ())
            self.assertEqual(drive_exit_code(first), 0)

            # 自愈补出的是「待领用确认」待办，还没人点：读侧口径就是 STATE（待人工），
            # 不是证据不足 —— 这一点也必须留在「待人工」里，并且不算失败。
            original_read_event = harness.reader.read_event

            def open_todo(loan, source):
                if source == ISSUE_TASK:
                    raise ContractError(Code.STATE)
                return original_read_event(loan, source)

            harness.reader.read_event = open_todo
            for _ in range(2):
                again = DriveLoop(harness.engine, sources, harness.journal, harness.locks).run()
                self.assertEqual(again.recovered, ())
                self.assertEqual(again.blocked, ())
                self.assertEqual([o.code for o in again.skipped],
                                 [Code.DUPLICATE.value, Code.STATE.value])
                lines = '\n'.join(format_drive_lines(again))
                self.assertIn('待人工 1 条：阶段待办未完成 1 条（synthetic-issue-todo）', lines)
                # 稳态不报警：重复事件与开着的阶段待办都不是「要人看」。
                self.assertEqual(drive_exit_code(again), 0)
                self.assertEqual(harness.writer.writes, 2)
                self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state,
                                 State.AWAITING_ISSUE)
                self.assertEqual(
                    (harness.writer.ledger_stock.available,
                     harness.writer.ledger_stock.reserved,
                     harness.writer.ledger_stock.borrowed),
                    (3, 2, 0),
                )
        finally:
            harness.stop()

    def test_a_stale_queue_row_does_not_starve_the_heal(self):
        """队列里残留一条读不通的单：只记一条跳过，同轮该补的预留照样补上。"""
        harness = DriveHarness(self.runtime, self.locks)
        try:
            self.approval_written(harness)
            stale_ref = Resource('record', 'synthetic-org', 'synthetic-loans',
                                 'synthetic-stale-loan')
            stale = replace(fixtures.loan(), ref=stale_ref, config_version='stale-config')
            harness.reader.set_loan(stale)
            harness.writer.register(stale)
            harness.reader.set_event(stale_ref, fixtures.FORM,
                                     harness.event(Action.APPROVE))

            report = DriveLoop(
                harness.engine,
                StaticSources((
                    WorkItem('event', stale_ref, fixtures.FORM),
                    WorkItem('event', fixtures.LOAN, fixtures.FORM),
                )),
                harness.journal, harness.locks).run()

            # 残留条目按 CONFIG 记跳过（不是挂起），自愈没被它饿死。
            self.assertEqual([o.code for o in report.skipped if o.loan_id == 'synthetic-stale-loan'],
                             [Code.CONFIG.value] * 2)
            self.assertEqual([o.code for o in report.blocked], [])
            self.assertEqual(harness.reader.read_loan(fixtures.LOAN).state, State.AWAITING_ISSUE)
            self.assertEqual(
                (harness.writer.ledger_stock.available, harness.writer.ledger_stock.reserved,
                 harness.writer.ledger_stock.borrowed),
                (3, 2, 0),
            )
            # CONFIG 不是稳态：退出码要人看。
            self.assertEqual(drive_exit_code(report), 1)
        finally:
            harness.stop()

    def test_requery_without_a_conclusion_is_a_hang_never_a_recovery(self):
        """回查查不出结论：不算「已回查」，落成自描述挂起（旧行为同一个 op 两头都算）。"""
        harness = DriveHarness(self.runtime, self.locks)
        harness.writer.lose_response = True
        harness.reader.set_event(fixtures.LOAN, fixtures.FORM, harness.event(Action.APPROVE))
        first = DriveLoop(
            harness.engine, StaticSources((WorkItem('event', fixtures.LOAN, fixtures.FORM),)),
            harness.journal, harness.locks).run()
        self.assertEqual([o.code for o in first.blocked], [Code.UNKNOWN.value])
        self.assertEqual(harness.writer.writes, 1)
        operation_id = harness.journal.unresolved_ids()[0]

        def carry_over(target):
            target.writer.loan_states = dict(harness.writer.loan_states)
            target.writer.current = harness.writer.current
            target.writer.inventory = harness.writer.inventory
            target.writer.ledger_stock = harness.writer.ledger_stock
            target.writer.writes = harness.writer.writes
            target.reader.seed_loans = dict(harness.reader.seed_loans)
            target.reader.events = dict(harness.reader.events)
        harness.stop()

        # 平台回查仍然说不清（真机形态：query 回未知，不猜落没落）。
        stuck = DriveHarness(self.runtime, self.locks)
        carry_over(stuck)
        stuck.writer.query = lambda intent: Receipt(intent.operation_id, Outcome.UNKNOWN)
        try:
            report = DriveLoop(stuck.engine, StaticSources(()), stuck.journal, stuck.locks).run()

            self.assertEqual(report.recovered, ())
            hangs = [o for o in report.blocked if o.kind == 'recover']
            self.assertEqual(len(hangs), 1)
            self.assertEqual(hangs[0].code, Hang.UNRESOLVED_WRITE.value)
            self.assertEqual(hangs[0].loan_id, fixtures.LOAN.resource_id)
            self.assertEqual(hangs[0].source_kind, fixtures.FORM.kind)
            # 明细行要说得出是哪条流水没结清（运维要拿它去 runtime/operations 里查）。
            self.assertIn(operation_id, hangs[0].note)
            lines = '\n'.join(format_drive_lines(report))
            self.assertIn('回查 0，', lines)
            self.assertIn(f'挂起 2（{Hang.UNRESOLVED_WRITE.value}×1，'
                          f'{Code.UNKNOWN.value}×1）', lines)
            self.assertIn('不算已回查', lines)
            self.assertIn('待人工 2 条', lines)
            # 同单那条队列条目被拦下的理由是「这张单有未决写」—— 这条理由这时是真话。
            self.assertIn('挂起 event loan=synthetic-loan form=synthetic-form '
                          f'{Code.UNKNOWN.value}', lines)
            self.assertEqual(drive_exit_code(report), 1)
            self.assertEqual(stuck.writer.writes, 1)
            self.assertEqual(stuck.journal.unresolved_ids(), (operation_id,))
        finally:
            stuck.stop()

        # 平台回查说得清了：这一轮才算「回查 1」，挂起消失。
        back = DriveHarness(self.runtime, self.locks)
        carry_over(back)
        try:
            healed = DriveLoop(back.engine, StaticSources(()), back.journal, back.locks).run()
            self.assertEqual(healed.recovered, (operation_id,))
            self.assertEqual(healed.blocked, ())
            lines = '\n'.join(format_drive_lines(healed))
            self.assertIn('回查 1，', lines)
            self.assertIn('挂起 0。', lines)
            # 结清之后这一轮照常按已落库的审批把预留补齐：回查只解掉「说不清」，
            # 不代替业务动作。
            self.assertEqual(back.writer.writes, 2)
            self.assertEqual(back.reader.read_loan(fixtures.LOAN).state, State.AWAITING_ISSUE)
            self.assertEqual(
                (back.writer.ledger_stock.available, back.writer.ledger_stock.reserved),
                (3, 2),
            )
        finally:
            back.stop()


if __name__ == '__main__':
    unittest.main()
