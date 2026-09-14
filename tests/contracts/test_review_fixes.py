"""Synthetic negative vectors for stage gating, ledger scope and journal intents."""
import unittest
import importlib.util
from pathlib import Path
from dataclasses import replace

from contracts.flow import plan
from contracts.model import Action, Code, ContractError, Outcome, State
from contracts.ports import (RuntimeBinding, check_binding, StageRequest,
                             LedgerScope, StageReceipt, lease_key)
spec = importlib.util.spec_from_file_location("t02_fixtures", Path(__file__).with_name("fixtures.py"))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
BORROWER, MANAGER, ITEM, LOAN, NOW = (fixtures.BORROWER, fixtures.MANAGER, fixtures.ITEM,
                                      fixtures.LOAN, fixtures.NOW)
loan, stock, event = fixtures.loan, fixtures.stock, fixtures.event
completion, settled = fixtures.completion, fixtures.settled

synthetic_spec = importlib.util.spec_from_file_location("t02_synthetic", Path(__file__).with_name("synthetic.py"))
synthetic = importlib.util.module_from_spec(synthetic_spec)
synthetic_spec.loader.exec_module(synthetic)
SyntheticJournal, SyntheticLease = synthetic.SyntheticJournal, synthetic.SyntheticLease

SCOPE = LedgerScope.from_record(ITEM)


def stage_request(current, action, actor=MANAGER, operation_id="synthetic-stage-op"):
    return StageRequest(operation_id, current, action, actor)


class ReviewFixTests(unittest.TestCase):
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

    def issued(self):
        current, inventory = self.reserved()
        return settled(current, completion(Action.ISSUE), inventory)

    # Spec1: stage creation requires an allowed predecessor business state.
    def test_issue_stage_requires_reservation(self):
        self.blocked(Code.STATE, lambda: stage_request(self.approved()[0], Action.ISSUE))

    def test_return_stage_requires_return_request_first(self):
        self.blocked(Code.STATE, lambda: stage_request(self.issued()[0], Action.RETURN))
        current, _ = self.issued()
        current = replace(current, state=State.AWAITING_RETURN)
        stage_request(current, Action.RETURN)

    def test_return_request_stage_blocked_for_unissued(self):
        self.blocked(Code.STATE, lambda: stage_request(self.approved()[0],
                     Action.REQUEST_RETURN, BORROWER))

    def test_stage_on_rejected_closed_borrowed_or_reserved(self):
        rejected, _ = settled(loan(), event(Action.REJECT), stock())
        for action in (Action.APPROVE, Action.ISSUE, Action.RETURN):
            self.blocked(Code.STATE, lambda a=action: stage_request(rejected, a))
        current, _ = self.reserved()
        self.blocked(Code.STATE, lambda: stage_request(
            replace(current, state=State.CLOSED), Action.APPROVE))
        self.blocked(Code.STATE, lambda: stage_request(self.issued()[0], Action.APPROVE))
        self.blocked(Code.STATE, lambda: stage_request(self.reserved()[0],
                     Action.REQUEST_RETURN, BORROWER))

    def test_approve_and_cancel_stages_still_allowed_before_issue(self):
        stage_request(loan(), Action.APPROVE)
        stage_request(self.reserved()[0], Action.CANCEL)

    # Spec2: binding checks the ledger scope, not one item record.
    def test_same_ledger_different_item_accepted_other_ledger_rejected(self):
        other = replace(ITEM, resource_id="synthetic-other-item")
        binding = RuntimeBinding(MANAGER, SCOPE, "synthetic-config-v1", MANAGER, MANAGER,
                                 "synthetic-binding-readback", True, True, True, True)
        check_binding(binding, loan())
        check_binding(binding, replace(loan(), item=other))
        # Tenant mismatch is rejected by the Loan model itself, so the
        # binding-level rejection is proven with a same-tenant foreign table.
        self.blocked(Code.WRONG_LOAN, lambda: check_binding(binding, replace(
            loan(), item=replace(other, container_id="synthetic-other-table"))))

    def test_lock_key_is_scope_not_item_or_account(self):
        other_item = replace(ITEM, resource_id="synthetic-other-item")
        self.assertEqual(lease_key(LedgerScope.from_record(other_item)), lease_key(SCOPE))
        self.assertNotEqual(lease_key(LedgerScope("synthetic-org",
                                                  "synthetic-base+synthetic-other-table")),
                            lease_key(SCOPE))
        leases = SyntheticLease()
        first = leases.acquire(SCOPE, MANAGER)
        self.blocked(Code.INSTANCE, lambda: leases.acquire(SCOPE, BORROWER))
        self.blocked(Code.INSTANCE, lambda: leases.acquire(
            LedgerScope.from_record(other_item), MANAGER))
        other_scope = LedgerScope("synthetic-org", "synthetic-base+synthetic-other-table")
        second = leases.acquire(other_scope, MANAGER)
        self.assertNotEqual(first, second)
        leases.assert_held(first)
        leases.assert_held(second)
        leases.release(first)
        self.blocked(Code.INSTANCE, lambda: leases.assert_held(first))

    # Spec3: journal must support both published intent types.
    def test_journal_supports_stage_and_write_intents(self):
        journal = SyntheticJournal()
        request = stage_request(self.reserved()[0], Action.ISSUE,
                                operation_id="synthetic-stage-persist")
        journal.prepare(request)
        loaded, receipt = journal.load(request.operation_id)
        self.assertIs(loaded, request)
        self.assertIsNone(receipt)
        other_loan = replace(loan(), ref=replace(LOAN, resource_id="synthetic-other-loan"))
        intent = plan(other_loan, replace(event(Action.APPROVE),
                                          loan_ref=other_loan.ref), stock())
        journal.prepare(intent)
        loaded, receipt = journal.load(intent.operation_id)
        self.assertEqual(loaded, intent)
        self.assertIsNone(receipt)

    def test_journal_rejects_conflicting_stage_and_unknown_conflicts(self):
        journal = SyntheticJournal()
        current, inventory = self.reserved()
        request = stage_request(current, Action.ISSUE,
                                operation_id="synthetic-stage-conflict")
        journal.prepare(request)
        self.blocked(Code.UNKNOWN, lambda: journal.prepare(
            stage_request(current, Action.CANCEL, operation_id="synthetic-other-stage")))
        intent = plan(current, event(Action.CANCEL), inventory)
        self.blocked(Code.UNKNOWN, lambda: journal.prepare(intent))
        journal.save_receipt(StageReceipt(request.operation_id, Outcome.UNKNOWN))
        self.blocked(Code.UNKNOWN, lambda: journal.prepare(
            stage_request(current, Action.CANCEL, operation_id="synthetic-other-stage-2")))
        journal.save_receipt(StageReceipt(request.operation_id, Outcome.VERIFIED,
                                          source=fixtures.FORM))
        current = replace(current, state=State.CANCELLED)
        journal.prepare(intent)


if __name__ == "__main__":
    unittest.main()
