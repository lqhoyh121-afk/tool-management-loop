"""L1/L2 lending drive over FileJournal. SYNTHETIC ports only; no live DingTalk."""
import importlib.util
import io
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
from bootstrap.drive import (DriveLoop, FileSources, StaticSources, WorkItem,
                             dump_sources, format_drive_lines, run_bound_drive)
from bootstrap.instance import MachineLock
from bootstrap.journal import FileJournal
from bootstrap.wizard import main
from contracts.model import Action, Code, ContractError, Identity, IdentityBinding, Inventory, Resource, State
from contracts.ports import LedgerScope, RuntimeBinding, StageReceipt, stage_operation_id
from contracts.flow import Outcome
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
            self.assertEqual(report.blocked[0].code, Code.STATE.value)
            self.assertIn(
                '挂起 event loan=synthetic-loan form=synthetic-form INVALID_STATE',
                '\n'.join(format_drive_lines(report)),
            )
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
        from bootstrap.drive import DriveOutcome, DriveReport, format_drive_lines

        waiting = DriveOutcome('event', 'recLoan', 'todo', 'task-1', Code.STATE.value)
        broken = DriveOutcome('event', 'recLoan', 'form', 'row-1', 'EVIDENCE_REQUIRED')
        lines = '\n'.join(format_drive_lines(DriveReport((), (), (waiting, broken), ())))

        self.assertIn('待人工 1 条', lines)
        self.assertIn('不是证据不足', lines)
        self.assertIn('task-1', lines)
        self.assertIn('EVIDENCE_REQUIRED×1', lines)
        self.assertIn(f'{Code.STATE.value}×1', lines)

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

    def test_broken_title_display_blocked_as_config(self):
        from bootstrap.drive import title_display_from_document
        for broken in ({'item_name_field': 7},
                       {'borrower_names': 'yes'},
                       {'approve_entry_url': ['https://example.invalid/x']},
                       'synthetic-not-a-block'):
            self.blocked(Code.CONFIG, lambda broken=broken: title_display_from_document(
                binding_document(title_display=broken)))


if __name__ == '__main__':
    unittest.main()
