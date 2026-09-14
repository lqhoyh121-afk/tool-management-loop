"""All identities/resources in this suite are synthetic, not business data."""
import unittest
import importlib.util
from pathlib import Path
from dataclasses import replace

from contracts.model import Resource, Inventory
from contracts.flow import plan, verify, State, Action, Receipt, Outcome


spec = importlib.util.spec_from_file_location("t02_fixtures", Path(__file__).with_name("fixtures.py"))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
loan, event, stock = fixtures.loan, fixtures.event, fixtures.stock
completion, settled = fixtures.completion, fixtures.settled
BORROWER, ITEM = fixtures.BORROWER, fixtures.ITEM


class FlowTests(unittest.TestCase):
    def test_whole_loan_and_return_requires_independent_readback(self):
        current, inventory = settled(loan(), event(Action.APPROVE), stock())
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind="system")
        current, inventory = settled(current, reserve, inventory)
        self.assertEqual((current.state, inventory.available, inventory.reserved),
                         (State.AWAITING_ISSUE, 3, 2))
        current, inventory = settled(current, completion(Action.ISSUE), inventory)
        self.assertEqual((current.state, inventory.reserved, inventory.borrowed),
                         (State.BORROWED, 0, 2))
        request = replace(event(Action.REQUEST_RETURN, BORROWER), quantity=2,
                          return_ref=Resource("record", "synthetic-org", "synthetic-returns", "synthetic-return"))
        current, inventory = settled(current, request, inventory)
        self.assertEqual((current.state, inventory.available), (State.AWAITING_RETURN, 3))
        intent = plan(current, completion(Action.RETURN), inventory)
        unknown = verify(intent, Receipt(intent.operation_id, Outcome.UNKNOWN))
        self.assertEqual((unknown.loan.state, unknown.outcome),
                         (State.AWAITING_RETURN, Outcome.UNKNOWN))
        result = verify(intent, Receipt(intent.operation_id, Outcome.VERIFIED,
                                       "synthetic-independent-get", intent.after, intent.stock_after))
        self.assertEqual((result.loan.state, intent.stock_after.available,
                          intent.stock_after.borrowed), (State.CLOSED, 5, 0))

    def test_explicit_approval_does_not_reserve_or_issue(self):
        stock = Inventory(ITEM, 5, 0, 0, (), (), (), "synthetic-rev-1")
        intent = plan(loan(), event(Action.APPROVE), stock)
        self.assertEqual(intent.after.state, State.RESERVATION_PENDING)
        self.assertEqual(intent.stock_after.available, 5)
        receipt = Receipt(intent.operation_id, Outcome.VERIFIED, "synthetic-readback",
                          intent.after, intent.stock_after)
        result = verify(intent, receipt)
        self.assertEqual(result.loan.state, State.RESERVATION_PENDING)
        self.assertEqual(result.outcome, Outcome.VERIFIED)


if __name__ == "__main__":
    unittest.main()
