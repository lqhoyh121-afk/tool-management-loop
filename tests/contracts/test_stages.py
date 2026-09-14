"""Synthetic stage-resource contract tests; no TODO or form is created."""
from dataclasses import replace
import unittest
import importlib.util
from pathlib import Path

from contracts.model import Action, Outcome, Code, ContractError
from contracts.ports import StageRequest, StageReceipt, verify_stage, stage_operation_id
spec = importlib.util.spec_from_file_location("t02_fixtures", Path(__file__).with_name("fixtures.py"))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
loan, completion = fixtures.loan, fixtures.completion
MANAGER, BORROWER, FORM = fixtures.MANAGER, fixtures.BORROWER, fixtures.FORM


class StageTests(unittest.TestCase):
    def test_stable_stage_identity_is_distinct_from_business_action(self):
        self.assertEqual(stage_operation_id(loan(), Action.ISSUE),
                         stage_operation_id(loan(), Action.ISSUE))
        self.assertNotEqual(stage_operation_id(loan(), Action.ISSUE),
                            stage_operation_id(loan(), Action.RETURN))

    def test_stage_requires_both_creation_and_readback(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE), loan(), Action.APPROVE, MANAGER)
        receipt = StageReceipt(request.operation_id, Outcome.VERIFIED, FORM,
                               creation_evidence="synthetic-create", readback_evidence="synthetic-read")
        self.assertEqual(verify_stage(request, receipt), Outcome.VERIFIED)
        self.assertEqual(verify_stage(request, replace(receipt, creation_evidence="")), Outcome.UNKNOWN)
        self.assertEqual(verify_stage(request, replace(receipt, source=None)), Outcome.UNKNOWN)

    def test_todo_mapping_must_match_returned_task_and_actor(self):
        request = StageRequest(stage_operation_id(loan(), Action.ISSUE), loan(), Action.ISSUE, MANAGER)
        done = completion(Action.ISSUE)
        receipt = StageReceipt(request.operation_id, Outcome.VERIFIED, done.source,
                               done.binding, "synthetic-create", "synthetic-read")
        self.assertEqual(verify_stage(request, receipt), Outcome.VERIFIED)
        self.assertEqual(verify_stage(request, replace(receipt, binding=None)), Outcome.UNKNOWN)
        self.assertEqual(verify_stage(request, replace(receipt, source=FORM)), Outcome.UNKNOWN)

    def test_stage_wrong_actor_and_operation_rejected(self):
        with self.assertRaises(ContractError) as caught:
            StageRequest("synthetic-stage", loan(), Action.APPROVE, BORROWER)
        self.assertEqual(caught.exception.code, Code.WRONG_PERSON)
        request = StageRequest("synthetic-stage", loan(), Action.APPROVE, MANAGER)
        with self.assertRaises(ContractError) as caught:
            verify_stage(request, StageReceipt("synthetic-wrong", Outcome.UNKNOWN))
        self.assertEqual(caught.exception.code, Code.OP_CONFLICT)
