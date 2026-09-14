"""L2 synthetic module integration + independent process ID stability.

These tests do not prove OS exclusivity, durable recovery storage or DingTalk.
"""
from dataclasses import replace
import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from contracts.flow import plan, verify, operation_id
from contracts.model import Action, Code, ContractError, Outcome, State
from contracts.ports import LedgerScope, RuntimeBinding
fixture_spec = importlib.util.spec_from_file_location("t02_fixtures", Path(__file__).with_name("fixtures.py"))
fixtures = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixtures)
loan, stock, event, completion = fixtures.loan, fixtures.stock, fixtures.event, fixtures.completion
MANAGER, ITEM, BORROWER, LOAN = fixtures.MANAGER, fixtures.ITEM, fixtures.BORROWER, fixtures.LOAN
synthetic_spec = importlib.util.spec_from_file_location("t02_synthetic", Path(__file__).with_name("synthetic.py"))
synthetic = importlib.util.module_from_spec(synthetic_spec)
synthetic_spec.loader.exec_module(synthetic)
SyntheticJournal, SyntheticLease, SyntheticWriter = (synthetic.SyntheticJournal,
                                                     synthetic.SyntheticLease, synthetic.SyntheticWriter)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.binding = RuntimeBinding(MANAGER, LedgerScope.from_record(ITEM),
                                      "synthetic-config-v1", MANAGER,
                                      MANAGER, "synthetic-readback", True, True, True, True)
        self.writer = SyntheticWriter(loan(), stock(), self.journal, self.leases)

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_unknown_response_queries_original_intent_without_resending(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.writer.lose_response = True
        receipt = self.writer.submit(intent, self.binding, self.lease)
        self.assertEqual(verify(intent, receipt).outcome, Outcome.UNKNOWN)
        self.blocked(Code.UNKNOWN, lambda: self.writer.submit(intent, self.binding, self.lease))
        original, saved_receipt = self.journal.load(intent.operation_id)
        self.assertEqual(saved_receipt.outcome, Outcome.UNKNOWN)
        recovered = verify(original, self.writer.query(original))
        self.assertEqual((recovered.loan.state, recovered.outcome, self.writer.writes),
                         (State.RESERVATION_PENDING, Outcome.VERIFIED, 1))
        self.writer.submit(original, self.binding, self.lease)
        self.assertEqual(self.writer.writes, 1)

    def test_whole_chain_through_injected_ports(self):
        actions = [event(Action.APPROVE),
                   replace(event(Action.RESERVE), actor=None, evidence_kind="system"),
                   completion(Action.ISSUE),
                   replace(event(Action.REQUEST_RETURN, BORROWER), quantity=2,
                           return_ref=replace(LOAN, resource_id="synthetic-return")),
                   completion(Action.RETURN)]
        for action_event in actions:
            intent = plan(self.writer.current, action_event, self.writer.inventory)
            receipt = self.writer.submit(intent, self.binding, self.lease)
            self.assertEqual(verify(intent, receipt).outcome, Outcome.VERIFIED)
        self.assertEqual((self.writer.current.state, self.writer.inventory.available,
                          self.writer.inventory.borrowed, self.writer.writes),
                         (State.CLOSED, 5, 0, 5))

    def test_duplicate_reservation_does_not_decrement_twice(self):
        approved = plan(loan(), event(Action.APPROVE), stock())
        self.writer.submit(approved, self.binding, self.lease)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind="system")
        intent = plan(self.writer.current, reserve, self.writer.inventory)
        self.writer.submit(intent, self.binding, self.lease)
        self.writer.submit(intent, self.binding, self.lease)
        self.assertEqual((self.writer.inventory.available, self.writer.inventory.reserved,
                          self.writer.writes), (3, 2, 2))

    def test_partial_readback_remains_unknown_and_blocks_cancel(self):
        approved = plan(loan(), event(Action.APPROVE), stock())
        self.writer.submit(approved, self.binding, self.lease)
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind="system")
        intent = plan(self.writer.current, reserve, self.writer.inventory)
        self.writer.lose_response = True
        self.writer.submit(intent, self.binding, self.lease)
        # Simulate only one of the two external resources having been written.
        self.writer.inventory = intent.stock_before
        self.assertEqual(self.writer.query(intent).outcome, Outcome.UNKNOWN)
        cancel = plan(intent.before, event(Action.CANCEL), intent.stock_before)
        self.blocked(Code.UNKNOWN, lambda: self.writer.submit(cancel, self.binding, self.lease))
        self.assertEqual(self.writer.writes, 2)

    def test_same_operation_different_payload_rejected(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.journal.prepare(intent)
        self.blocked(Code.OP_CONFLICT, lambda: self.journal.prepare(
            replace(intent, stock_before=replace(stock(), available=4))))

    def test_unresolved_operation_blocks_another_action(self):
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.journal.prepare(intent)
        cancel = plan(loan(), event(Action.CANCEL), stock())
        self.blocked(Code.UNKNOWN, lambda: self.journal.prepare(cancel))

    def test_stale_inventory_cannot_overreserve(self):
        self.writer.inventory = replace(stock(), available=1)
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.blocked(Code.CONFLICT, lambda: self.writer.submit(intent, self.binding, self.lease))
        self.assertEqual(self.writer.writes, 0)

    def test_second_instance_and_lost_lease_fail_closed(self):
        self.blocked(Code.INSTANCE, lambda: self.leases.acquire(
            LedgerScope.from_record(ITEM), BORROWER))
        self.leases.release(self.lease)
        intent = plan(loan(), event(Action.APPROVE), stock())
        self.blocked(Code.INSTANCE, lambda: self.writer.submit(intent, self.binding, self.lease))
        self.assertEqual(self.writer.writes, 0)

    def test_operation_id_survives_process_and_json_handoff(self):
        root = Path(__file__).resolve().parents[2]
        expected = operation_id(loan(), event(Action.APPROVE))
        with tempfile.TemporaryDirectory(prefix="synthetic-contract-") as temp:
            saved = Path(temp) / "operation.json"
            saved.write_text(json.dumps({"operation_id": expected}), encoding="utf-8")
            code = ("import json,sys; from pathlib import Path; "
                    "sys.path.insert(0,str(Path.cwd()/'tests'/'contracts')); "
                    "from fixtures import loan,event; from contracts.flow import operation_id; "
                    "from contracts.model import Action; "
                    "actual=operation_id(loan(),event(Action.APPROVE)); "
                    "assert actual==json.loads(Path(sys.argv[1]).read_text())['operation_id']; "
                    "print(actual)")
            completed = subprocess.run([sys.executable, "-B", "-c", code, str(saved)],
                                       cwd=root, capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
