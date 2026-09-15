"""All identities/resources are synthetic; ports are in-memory stand-ins (L1/L2 only)."""
import importlib.util
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from contracts.model import (Action, Code, ContractError, Identity, IdentityBinding,
                             Inventory, Resource, State)
from contracts.ports import LedgerScope, RuntimeBinding, StageReceipt
from contracts.flow import Outcome
from workflow.engine import LendingEngine


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / 'contracts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


synthetic = _load_module('wf_t02_synthetic', 'synthetic.py')
fixtures = _load_module('wf_t02_fixtures', 'fixtures.py')

RETURN = Resource('record', 'synthetic-org', 'synthetic-returns', 'synthetic-return-1')
ISSUE_TASK = Resource('todo', 'synthetic-org', 'synthetic-todos', 'synthetic-issue-todo')
RETURN_TASK = Resource('todo', 'synthetic-org', 'synthetic-todos', 'synthetic-return-todo')
INTERNAL_MANAGER = Identity('todo', 'synthetic-org', 'synthetic-internal-manager')


class SyntheticReader:
    def __init__(self):
        self.loans = {}
        self.inventory = None
        self.events = {}

    def set_loan(self, loan):
        self.loans[loan.ref.resource_id] = loan

    def set_event(self, loan_ref, source, event):
        self.events[(loan_ref, source)] = event

    def read_loan(self, ref):
        return self.loans[ref.resource_id]

    def read_inventory(self, ref):
        if self.inventory is None or self.inventory.ref != ref:
            raise ContractError(Code.WRONG_LOAN)
        return self.inventory

    def read_event(self, loan, source):
        return self.events[(loan.ref, source)]


class SyntheticStages:
    """SYNTHETIC stage port; records creations, answers later queries."""

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
    """SYNTHETIC writer extended to one ledger shared by several loans."""

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


class EngineHarness:
    def __init__(self, tracked=False, available=5, quantity=2):
        self.reader = SyntheticReader()
        self.journal = synthetic.SyntheticJournal()
        self.leases = synthetic.SyntheticLease()
        self.writer = SharedLedgerWriter(self.journal, self.leases)
        self.stages = SyntheticStages()
        self.binding = RuntimeBinding(
            fixtures.MANAGER, LedgerScope.from_record(fixtures.ITEM),
            'synthetic-config-v1', fixtures.MANAGER, fixtures.MANAGER,
            'synthetic-binding-readback', True, True, True, True)
        self.engine = LendingEngine(self.reader, self.writer, self.stages,
                                    self.journal, self.leases, self.binding)
        self.engine.start(LedgerScope.from_record(fixtures.ITEM), fixtures.MANAGER)
        self.tracked = tracked
        self.quantity = quantity
        self.available = available

    def seed(self, state=State.AWAITING_APPROVAL):
        loan = fixtures.loan()
        if self.tracked:
            loan = replace(loan, tracked=True,
                           physical_ids=('synthetic-unit-1', 'synthetic-unit-2'))
        loan = replace(loan, state=state)
        if self.tracked:
            stock = Inventory(fixtures.ITEM, self.quantity, 0, 0,
                              ('synthetic-unit-1', 'synthetic-unit-2'), (), (),
                              'synthetic-rev-1')
        else:
            stock = Inventory(fixtures.ITEM, self.available, 0, 0, (), (), (),
                              'synthetic-rev-1')
        self.register(loan, stock)
        return loan

    def register(self, loan, stock=None):
        self.reader.set_loan(loan)
        self.writer.register(loan)
        if stock is not None:
            self.reader.inventory = stock
            self.writer.ledger_stock = stock

    def stock(self):
        return self.reader.read_inventory(fixtures.ITEM)

    def counts(self):
        stock = self.stock()
        return (stock.available, stock.reserved, stock.borrowed)

    def event(self, action, actor=None, **changes):
        base = fixtures.event(action, actor or fixtures.MANAGER)
        base = replace(base, occurred_at=fixtures.NOW - timedelta(hours=1))
        return replace(base, **changes) if changes else base

    def todo_event(self, action, task):
        binding = IdentityBinding(fixtures.MANAGER, INTERNAL_MANAGER, task,
                                  'synthetic-create', 'synthetic-get')
        return replace(self.event(action), actor=INTERNAL_MANAGER, source=task,
                       evidence_kind='todo_completion', binding=binding)

    def sync(self, resolution):
        if getattr(resolution, 'outcome', None) == Outcome.VERIFIED:
            self.reader.set_loan(resolution.loan)
            self.reader.inventory = self.writer.ledger_stock
        return resolution

    def stage(self, loan, action):
        return self.engine.ensure_stage(loan, action)

    def run(self, source, event):
        self.reader.set_event(event.loan_ref, source, event)
        return self.sync(self.engine.execute(event.loan_ref, source))


class WorkflowTests(unittest.TestCase):
    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)

    def drive_full_loop(self, tracked=False):
        harness = EngineHarness(tracked=tracked)
        loan = harness.seed()
        total = harness.reader.read_inventory(loan.item).available
        self.assertEqual(harness.stage(loan, Action.APPROVE).outcome, Outcome.VERIFIED)

        approve = harness.event(Action.APPROVE)
        self.assertEqual(harness.run(fixtures.FORM, approve).outcome, Outcome.VERIFIED)
        self.assertEqual(harness.reader.read_loan(loan.ref).state, State.RESERVATION_PENDING)
        self.assertEqual(harness.counts(), (total, 0, 0))

        self.assertEqual(harness.sync(harness.engine.reserve(loan.ref, approve)).outcome,
                         Outcome.VERIFIED)
        pending = harness.reader.read_loan(loan.ref)
        self.assertEqual(pending.state, State.AWAITING_ISSUE)
        self.assertEqual(harness.counts(), (total - 2, 2, 0))
        if tracked:
            self.assertEqual(harness.stock().reserved_ids, ('synthetic-unit-1', 'synthetic-unit-2'))

        self.assertEqual(harness.stage(pending, Action.ISSUE).outcome, Outcome.VERIFIED)
        issue = harness.todo_event(Action.ISSUE, ISSUE_TASK)
        self.assertEqual(harness.run(ISSUE_TASK, issue).outcome, Outcome.VERIFIED)
        borrowed = harness.reader.read_loan(loan.ref)
        self.assertEqual(borrowed.state, State.BORROWED)
        self.assertEqual(harness.counts(), (total - 2, 0, 2))
        if tracked:
            self.assertEqual(harness.stock().borrowed_ids, ('synthetic-unit-1', 'synthetic-unit-2'))

        self.assertEqual(harness.stage(borrowed, Action.REQUEST_RETURN).outcome,
                         Outcome.VERIFIED)
        request = harness.event(Action.REQUEST_RETURN, actor=fixtures.BORROWER,
                                return_ref=RETURN, quantity=2,
                                physical_ids=borrowed.physical_ids)
        self.assertEqual(harness.run(fixtures.FORM, request).outcome, Outcome.VERIFIED)
        awaiting_return = harness.reader.read_loan(loan.ref)
        self.assertEqual(awaiting_return.state, State.AWAITING_RETURN)
        self.assertEqual(awaiting_return.return_ref, RETURN)

        self.assertEqual(harness.stage(awaiting_return, Action.RETURN).outcome,
                         Outcome.VERIFIED)
        returned = harness.todo_event(Action.RETURN, RETURN_TASK)
        self.assertEqual(harness.run(RETURN_TASK, returned).outcome, Outcome.VERIFIED)
        self.assertEqual(harness.reader.read_loan(loan.ref).state, State.CLOSED)
        self.assertEqual(harness.counts(), (total, 0, 0))
        if tracked:
            self.assertEqual(harness.stock().available_ids, ('synthetic-unit-1', 'synthetic-unit-2'))
        self.assertEqual(harness.writer.writes, 5)
        self.assertEqual(len(harness.stages.created), 4)
        return harness

    def test_full_lending_loop(self):
        self.drive_full_loop()

    def test_tracked_units_move_and_return(self):
        self.drive_full_loop(tracked=True)

    def test_stage_created_once(self):
        harness = EngineHarness()
        loan = harness.seed()
        first = harness.stage(loan, Action.APPROVE)
        again = harness.stage(loan, Action.APPROVE)
        self.assertEqual(first, again)
        self.assertEqual(len(harness.stages.created), 1)

    def test_stage_precondition_blocks_early_issue_entry(self):
        harness = EngineHarness()
        loan = harness.seed()
        self.blocked(Code.STATE, lambda: harness.stage(loan, Action.ISSUE))

    def test_admit_application_validates_trusted_apply(self):
        harness = EngineHarness()
        harness.seed()
        apply_event = harness.event(Action.APPLY, actor=fixtures.BORROWER,
                                    quantity=2, physical_ids=())
        harness.reader.set_event(apply_event.loan_ref, fixtures.FORM, apply_event)
        admitted = harness.engine.admit_application(apply_event.loan_ref, fixtures.FORM)
        self.assertEqual(admitted.state, State.AWAITING_APPROVAL)
        self.assertEqual(admitted.consumed_events, (apply_event.event_id,))
        self.assertTrue(admitted.application_evidence.strip())

        bad = harness.event(Action.APPLY, actor=fixtures.BORROWER,
                            quantity=5, physical_ids=())
        harness.reader.set_event(bad.loan_ref, fixtures.FORM, bad)
        self.blocked(Code.QUANTITY,
                     lambda: harness.engine.admit_application(bad.loan_ref, fixtures.FORM))

    def test_apply_due_date_must_be_future(self):
        harness = EngineHarness()
        harness.seed()
        stale = replace(harness.event(Action.APPLY, actor=fixtures.BORROWER,
                                      quantity=2, physical_ids=()),
                        occurred_at=fixtures.NOW + timedelta(hours=1))
        harness.reader.set_event(stale.loan_ref, fixtures.FORM, stale)
        self.blocked(Code.INVALID,
                     lambda: harness.engine.admit_application(stale.loan_ref, fixtures.FORM))

    def test_rejection_blocks_everything_after(self):
        harness = EngineHarness()
        loan = harness.seed()
        harness.stage(loan, Action.APPROVE)
        self.assertEqual(harness.run(fixtures.FORM, harness.event(Action.REJECT)).outcome,
                         Outcome.VERIFIED)
        self.assertEqual(harness.reader.read_loan(loan.ref).state, State.REJECTED)
        approve = harness.event(Action.APPROVE)
        harness.reader.set_event(loan.ref, fixtures.FORM, approve)
        self.blocked(Code.STATE, lambda: harness.engine.execute(loan.ref, fixtures.FORM))
        self.blocked(Code.STATE, lambda: harness.engine.reserve(loan.ref, approve))
        self.assertEqual(harness.counts(), (5, 0, 0))

    def test_cancel_releases_reservation(self):
        harness = EngineHarness()
        loan = harness.seed()
        harness.stage(loan, Action.APPROVE)
        approve = harness.event(Action.APPROVE)
        harness.run(fixtures.FORM, approve)
        harness.sync(harness.engine.reserve(loan.ref, approve))
        self.assertEqual(harness.run(fixtures.FORM, harness.event(Action.CANCEL)).outcome,
                         Outcome.VERIFIED)
        self.assertEqual(harness.reader.read_loan(loan.ref).state, State.CANCELLED)
        self.assertEqual(harness.counts(), (5, 0, 0))

    def test_cancel_before_reservation_keeps_stock(self):
        harness = EngineHarness()
        loan = harness.seed()
        harness.run(fixtures.FORM, harness.event(Action.CANCEL))
        self.assertEqual(harness.counts(), (5, 0, 0))

    def test_cancel_after_borrowed_rejected(self):
        harness = EngineHarness()
        loan = harness.seed()
        harness.stage(loan, Action.APPROVE)
        approve = harness.event(Action.APPROVE)
        harness.run(fixtures.FORM, approve)
        harness.sync(harness.engine.reserve(loan.ref, approve))
        pending = harness.reader.read_loan(loan.ref)
        harness.stage(pending, Action.ISSUE)
        harness.run(ISSUE_TASK, harness.todo_event(Action.ISSUE, ISSUE_TASK))
        self.blocked(Code.STATE, lambda: harness.run(fixtures.FORM, harness.event(Action.CANCEL)))

    def test_wrong_person_blocked(self):
        harness = EngineHarness()
        loan = harness.seed()
        harness.stage(loan, Action.APPROVE)
        wrong = harness.event(Action.APPROVE, actor=fixtures.BORROWER)
        harness.reader.set_event(loan.ref, fixtures.FORM, wrong)
        self.blocked(Code.WRONG_PERSON, lambda: harness.engine.execute(loan.ref, fixtures.FORM))

    def test_issue_completion_wrong_binding_person_blocked(self):
        harness = EngineHarness()
        loan = harness.seed()
        harness.stage(loan, Action.APPROVE)
        approve = harness.event(Action.APPROVE)
        harness.run(fixtures.FORM, approve)
        harness.sync(harness.engine.reserve(loan.ref, approve))
        harness.stage(harness.reader.read_loan(loan.ref), Action.ISSUE)
        wrong_binding = IdentityBinding(fixtures.BORROWER, INTERNAL_MANAGER, ISSUE_TASK,
                                        'synthetic-create', 'synthetic-get')
        wrong = replace(harness.todo_event(Action.ISSUE, ISSUE_TASK), binding=wrong_binding)
        harness.reader.set_event(loan.ref, ISSUE_TASK, wrong)
        self.blocked(Code.WRONG_PERSON, lambda: harness.engine.execute(loan.ref, ISSUE_TASK))

    def test_wrong_loan_blocked(self):
        harness = EngineHarness()
        harness.seed()
        other = replace(harness.event(Action.APPROVE),
                        loan_ref=Resource('record', 'synthetic-org', 'synthetic-loans',
                                          'another-loan'))
        harness.reader.set_event(fixtures.LOAN, fixtures.FORM, other)
        self.blocked(Code.WRONG_LOAN,
                     lambda: harness.engine.execute(fixtures.LOAN, fixtures.FORM))

    def test_return_quantity_mismatch_blocked(self):
        harness = EngineHarness()
        loan = harness.seed()
        harness.stage(loan, Action.APPROVE)
        approve = harness.event(Action.APPROVE)
        harness.run(fixtures.FORM, approve)
        harness.sync(harness.engine.reserve(loan.ref, approve))
        pending = harness.reader.read_loan(loan.ref)
        harness.stage(pending, Action.ISSUE)
        harness.run(ISSUE_TASK, harness.todo_event(Action.ISSUE, ISSUE_TASK))
        harness.stage(harness.reader.read_loan(loan.ref), Action.REQUEST_RETURN)
        bad = harness.event(Action.REQUEST_RETURN, actor=fixtures.BORROWER,
                            return_ref=RETURN, quantity=1, physical_ids=())
        harness.reader.set_event(loan.ref, fixtures.FORM, bad)
        self.blocked(Code.QUANTITY, lambda: harness.engine.execute(loan.ref, fixtures.FORM))

    def test_tracked_loan_requires_identifiers(self):
        self.blocked(Code.IDENTIFIERS,
                     lambda: replace(fixtures.loan(), tracked=True, physical_ids=()))

    def test_duplicate_event_does_not_double_apply(self):
        harness = EngineHarness()
        loan = harness.seed()
        harness.stage(loan, Action.APPROVE)
        approve = harness.event(Action.APPROVE)
        harness.run(fixtures.FORM, approve)
        writes = harness.writer.writes
        harness.reader.set_event(loan.ref, fixtures.FORM, approve)
        self.blocked(Code.DUPLICATE, lambda: harness.engine.execute(loan.ref, fixtures.FORM))
        self.assertEqual(harness.writer.writes, writes)
        self.assertEqual(harness.counts(), (5, 0, 0))

    def test_reservation_conflict_suspends_without_oversell(self):
        harness = EngineHarness(available=2)
        first = harness.seed()
        harness.stage(first, Action.APPROVE)
        approve = harness.event(Action.APPROVE)
        harness.run(fixtures.FORM, approve)
        harness.sync(harness.engine.reserve(first.ref, approve))
        self.assertEqual(harness.counts(), (0, 2, 0))

        second = replace(fixtures.loan(),
                         ref=Resource('record', 'synthetic-org', 'synthetic-loans',
                                      'synthetic-loan-2'))
        harness.register(second)
        approve2 = replace(harness.event(Action.APPROVE), loan_ref=second.ref,
                           event_id='synthetic-approve-2')
        harness.run(fixtures.FORM, approve2)
        entries = len(harness.journal.entries)
        self.blocked(Code.CONFLICT, lambda: harness.engine.reserve(second.ref, approve2))
        self.assertEqual(len(harness.journal.entries), entries)
        self.assertEqual(harness.counts(), (0, 2, 0))

    def test_unknown_write_resolves_by_query_not_resend(self):
        harness = EngineHarness()
        loan = harness.seed()
        harness.stage(loan, Action.APPROVE)
        approve = harness.event(Action.APPROVE)
        harness.writer.lose_response = True
        harness.reader.set_event(loan.ref, fixtures.FORM, approve)
        execution = harness.engine.execute(loan.ref, fixtures.FORM)
        self.assertEqual(execution.outcome, Outcome.UNKNOWN)
        self.assertEqual(harness.writer.writes, 1)
        self.blocked(Code.UNKNOWN, lambda: harness.engine.execute(loan.ref, fixtures.FORM))
        resolution = harness.sync(harness.engine.resolve(execution.operation_id))
        self.assertEqual(resolution.outcome, Outcome.VERIFIED)
        self.assertEqual(harness.reader.read_loan(loan.ref).state, State.RESERVATION_PENDING)
        self.assertEqual(harness.writer.writes, 1)

    def test_restart_recovers_unknown_and_stages(self):
        harness = EngineHarness()
        loan = harness.seed()
        receipt = harness.stage(loan, Action.APPROVE)
        harness.writer.lose_response = True
        approve = harness.event(Action.APPROVE)
        harness.reader.set_event(loan.ref, fixtures.FORM, approve)
        unknown = harness.engine.execute(loan.ref, fixtures.FORM)
        self.assertEqual(unknown.outcome, Outcome.UNKNOWN)

        harness.engine.stop()
        restarted = LendingEngine(harness.reader, harness.writer, harness.stages,
                                  harness.journal, harness.leases, harness.binding)
        restarted.start(LedgerScope.from_record(fixtures.ITEM), fixtures.MANAGER)
        resolution = harness.sync(restarted.resolve(unknown.operation_id))
        self.assertEqual(resolution.outcome, Outcome.VERIFIED)
        self.assertEqual(harness.writer.writes, 1)
        again = restarted.ensure_stage(loan, Action.APPROVE)
        self.assertEqual(again.operation_id, receipt.operation_id)
        self.assertEqual(len(harness.stages.created), 1)

    def test_second_instance_blocked(self):
        harness = EngineHarness()
        other = LendingEngine(harness.reader, harness.writer, harness.stages,
                              harness.journal, harness.leases, harness.binding)
        self.blocked(Code.INSTANCE, lambda: other.start(
            LedgerScope.from_record(fixtures.ITEM), fixtures.MANAGER))


if __name__ == '__main__':
    unittest.main()
