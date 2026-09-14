"""Synthetic negative vectors at the public contract boundary."""
import unittest
from dataclasses import replace
from datetime import timedelta

from contracts.flow import accept_application, plan, verify, Receipt
from contracts.model import Action, Code, ContractError, Outcome, State, business_date
from contracts.ports import RuntimeBinding, check_binding
from test_flow import (BORROWER, MANAGER, ITEM, LOAN, NOW, Identity, Resource,
                       loan, stock, event, completion, settled)


class GuardTests(unittest.TestCase):
    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def approved(self):
        return settled(loan(), event(Action.APPROVE), stock())

    def reserved(self):
        current, inventory = self.approved()
        return settled(current, replace(event(Action.RESERVE), actor=None,
                                        evidence_kind="system"), inventory)

    def test_trusted_application(self):
        draft = replace(loan(), due_at=NOW + timedelta(days=2))
        application = replace(event(Action.APPLY, BORROWER), quantity=2)
        result = accept_application(draft, application)
        self.assertEqual(result.state, State.AWAITING_APPROVAL)
        self.assertEqual(result.consumed_events, ("synthetic-apply",))
        self.blocked(Code.EVIDENCE, lambda: accept_application(draft, replace(application, verified=False)))

    def test_untrusted_initial_record_cannot_reach_approval(self):
        self.blocked(Code.EVIDENCE, lambda: plan(replace(loan(), application_evidence=""),
                     event(Action.APPROVE), stock()))

    def test_boolean_event_quantity_is_not_a_single_item(self):
        self.blocked(Code.QUANTITY, lambda: replace(event(Action.APPLY, BORROWER), quantity=True))

    def test_application_quantity_and_due_date(self):
        self.blocked(Code.QUANTITY, lambda: accept_application(loan(), event(Action.APPLY, BORROWER)))
        self.blocked(Code.INVALID, lambda: accept_application(
            loan(), replace(event(Action.APPLY, BORROWER), quantity=2)))

    def test_missing_identity(self):
        self.blocked(Code.IDENTITY, lambda: plan(loan(), event(Action.APPROVE, None), stock()))

    def test_wrong_approver(self):
        self.blocked(Code.WRONG_PERSON, lambda: plan(loan(), event(Action.APPROVE, BORROWER), stock()))

    def test_wrong_loan_and_tenant(self):
        other = replace(LOAN, resource_id="synthetic-other")
        self.blocked(Code.WRONG_LOAN, lambda: plan(loan(), replace(event(Action.APPROVE), loan_ref=other), stock()))
        self.blocked(Code.WRONG_LOAN, lambda: plan(loan(), replace(event(Action.APPROVE),
                     source=replace(event(Action.APPROVE).source, tenant_id="synthetic-other-org")), stock()))

    def test_todo_checkbox_is_not_approval(self):
        self.blocked(Code.EVIDENCE, lambda: plan(loan(), completion(Action.APPROVE), stock()))

    def test_rejection_cannot_issue_or_reserve(self):
        current, inventory = settled(loan(), event(Action.REJECT), stock())
        self.assertEqual((current.state, inventory.available), (State.REJECTED, 5))
        self.blocked(Code.STATE, lambda: plan(current, completion(Action.ISSUE), inventory))
        self.blocked(Code.STATE, lambda: plan(current, replace(event(Action.RESERVE),
                     actor=None, evidence_kind="system"), inventory))

    def test_insufficient_quantity_explicitly_suspends_reservation(self):
        current, inventory = self.approved()
        self.blocked(Code.CONFLICT, lambda: plan(current, replace(event(Action.RESERVE),
                     actor=None, evidence_kind="system"), replace(inventory, available=1)))
        self.assertEqual(current.state, State.RESERVATION_PENDING)

    def test_duplicate_event_and_conflicting_decision(self):
        current, inventory = self.approved()
        self.blocked(Code.DUPLICATE, lambda: plan(current, event(Action.APPROVE), inventory))
        self.blocked(Code.STATE, lambda: plan(current, event(Action.REJECT), inventory))

    def test_missing_or_wrong_completion_binding(self):
        current, inventory = self.reserved()
        valid = completion(Action.ISSUE)
        self.blocked(Code.EVIDENCE, lambda: plan(current, replace(valid, binding=None), inventory))
        self.blocked(Code.WRONG_PERSON, lambda: plan(current, replace(valid, actor=BORROWER), inventory))
        self.blocked(Code.EVIDENCE, lambda: plan(current, replace(valid,
                     source=replace(valid.source, resource_id="synthetic-other-task")), inventory))

    def test_confirmation_requires_actual_activity_evidence(self):
        current, inventory = self.reserved()
        self.blocked(Code.EVIDENCE, lambda: plan(current, replace(completion(Action.ISSUE), evidence_ref=""), inventory))
        self.blocked(Code.EVIDENCE, lambda: plan(current, replace(completion(Action.ISSUE), verified=False), inventory))

    def test_cancel_releases_only_reserved_stock(self):
        current, inventory = self.reserved()
        intent = plan(current, event(Action.CANCEL), inventory)
        self.assertEqual((intent.after.state, intent.stock_after.available,
                          intent.stock_after.reserved), (State.CANCELLED, 5, 0))
        unresolved = verify(intent, Receipt(intent.operation_id, Outcome.UNKNOWN))
        self.assertEqual(unresolved.loan.state, State.AWAITING_ISSUE)
        self.assertEqual(inventory.available, 3)

    def test_cancel_before_reservation_has_no_stock_effect(self):
        for current in (loan(), self.approved()[0]):
            intent = plan(current, event(Action.CANCEL), stock())
            self.assertEqual(intent.stock_after, stock())
            self.assertEqual(intent.after.state, State.CANCELLED)

    def test_cancel_wrong_person_and_after_issue(self):
        current, inventory = self.reserved()
        self.blocked(Code.WRONG_PERSON, lambda: plan(current, event(Action.CANCEL, BORROWER), inventory))
        current, inventory = settled(current, completion(Action.ISSUE), inventory)
        self.blocked(Code.STATE, lambda: plan(current, event(Action.CANCEL), inventory))

    def test_return_requires_original_and_whole_quantity(self):
        current, inventory = self.reserved()
        current, inventory = settled(current, completion(Action.ISSUE), inventory)
        request = event(Action.REQUEST_RETURN, BORROWER)
        self.blocked(Code.WRONG_LOAN, lambda: plan(current, request, inventory))
        request = replace(request, return_ref=replace(LOAN, resource_id="synthetic-return"), quantity=1)
        self.blocked(Code.QUANTITY, lambda: plan(current, request, inventory))
        self.blocked(Code.WRONG_PERSON, lambda: plan(current, replace(request, actor=MANAGER), inventory))
        self.assertEqual(inventory.available, 3)

    def test_no_return_confirmation_without_return_request(self):
        current, inventory = self.reserved()
        current, inventory = settled(current, completion(Action.ISSUE), inventory)
        self.blocked(Code.STATE, lambda: plan(current, completion(Action.RETURN), inventory))

    def test_tracked_items_require_real_identifiers(self):
        self.blocked(Code.IDENTIFIERS, lambda: replace(loan(), tracked=True))
        self.blocked(Code.IDENTIFIERS, lambda: replace(loan(), tracked=True,
                     physical_ids=("synthetic-physical-a", "synthetic-physical-a")))

    def test_tracked_stock_conflict_and_roundtrip(self):
        identifiers = ("synthetic-physical-a", "synthetic-physical-b")
        current = replace(loan(), tracked=True, physical_ids=identifiers)
        inventory = replace(stock(), available=2, available_ids=identifiers)
        current, inventory = settled(current, event(Action.APPROVE), inventory)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind="system")
        other = replace(inventory, available_ids=("synthetic-physical-c", "synthetic-physical-d"))
        self.blocked(Code.CONFLICT, lambda: plan(current, reserve, other))
        current, inventory = settled(current, reserve, inventory)
        current, inventory = settled(current, completion(Action.ISSUE), inventory)
        request = replace(event(Action.REQUEST_RETURN, BORROWER), quantity=2, physical_ids=identifiers,
                          return_ref=replace(LOAN, resource_id="synthetic-return"))
        self.blocked(Code.QUANTITY, lambda: plan(current, replace(request, physical_ids=()), inventory))
        current, inventory = settled(current, request, inventory)
        current, inventory = settled(current, completion(Action.RETURN), inventory)
        self.assertEqual((current.state, inventory.available_ids, inventory.borrowed_ids),
                         (State.CLOSED, identifiers, ()))

    def test_invalid_quantity_and_naive_time(self):
        for value in (0, -1, True, 1.5, "2", None):
            with self.subTest(value=value):
                self.blocked(Code.QUANTITY, lambda: replace(loan(), quantity=value))
        self.blocked(Code.INVALID, lambda: replace(loan(), due_at=NOW.replace(tzinfo=None)))

    def test_business_date_uses_shanghai(self):
        self.assertEqual(str(business_date(NOW + timedelta(hours=16))), "2030-01-02")
        self.blocked(Code.INVALID, lambda: business_date(NOW.replace(tzinfo=None)))

    def test_inflight_config_never_silently_changes(self):
        self.blocked(Code.CONFIG, lambda: plan(loan(), replace(event(Action.APPROVE),
                     config_version="synthetic-config-v2"), stock()))

    def test_binding_is_fail_closed_and_roles_are_pinned(self):
        binding = RuntimeBinding(MANAGER, ITEM, "synthetic-config-v1", MANAGER, MANAGER,
                                 "synthetic-binding-readback", True, True, True, True)
        check_binding(binding, loan())
        self.blocked(Code.CONFIG, lambda: check_binding(replace(binding, manager=BORROWER), loan()))
        self.blocked(Code.EVIDENCE, lambda: check_binding(replace(binding, ordinary_ledger_denied=False), loan()))
        self.blocked(Code.EVIDENCE, lambda: check_binding(replace(binding, explicitly_confirmed=False), loan()))

    def test_acceptance_or_partial_write_never_advances(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        for receipt in (Receipt(intent.operation_id, Outcome.UNKNOWN),
                        Receipt(intent.operation_id, Outcome.VERIFIED),
                        Receipt(intent.operation_id, Outcome.VERIFIED, "synthetic-read", intent.after,
                                replace(stock(), available=4)),
                        Receipt(intent.operation_id, Outcome.NOT_APPLIED, "synthetic-partial", intent.after, stock())):
            with self.subTest(receipt=receipt):
                result = verify(intent, receipt)
                self.assertEqual((result.loan, result.outcome), (loan(), Outcome.UNKNOWN))

    def test_wrong_operation_id(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.blocked(Code.OP_CONFLICT, lambda: verify(intent, Receipt("synthetic-wrong", Outcome.UNKNOWN)))

    def test_conclusive_not_applied_and_not_sent_are_distinct(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.assertEqual(verify(intent, Receipt(intent.operation_id, Outcome.NOT_SENT)).outcome, Outcome.NOT_SENT)
        receipt = Receipt(intent.operation_id, Outcome.NOT_APPLIED, "synthetic-query", loan(), stock())
        self.assertEqual(verify(intent, receipt).outcome, Outcome.NOT_APPLIED)


if __name__ == "__main__":
    unittest.main()
