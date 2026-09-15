"""L1/L2: durable FileJournal. SYNTHETIC intents only; no DingTalk."""
import importlib.util
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.journal import FileJournal
from contracts.flow import Receipt, plan
from contracts.model import Action, Code, ContractError, Outcome
from contracts.ports import StageReceipt, StageRequest, stage_operation_id


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / 'contracts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = _load('t10_journal_fixtures', 'fixtures.py')


class JournalTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name) / 'operations'
        self.store = FileJournal(self.root)

    def tearDown(self):
        self._temp.cleanup()

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_prepare_load_round_trip_survives_new_instance(self):
        intent = plan(fixtures.loan(), fixtures.event(Action.APPROVE), fixtures.stock())
        self.store.prepare(intent)
        self.store.prepare(intent)
        other = FileJournal(self.root)
        loaded, receipt = other.load(intent.operation_id)
        self.assertEqual(loaded, intent)
        self.assertIsNone(receipt)

    def test_same_id_different_payload_rejected(self):
        intent = plan(fixtures.loan(), fixtures.event(Action.APPROVE), fixtures.stock())
        self.store.prepare(intent)
        other = plan(fixtures.loan(), fixtures.event(Action.REJECT), fixtures.stock())
        conflict = replace(other, operation_id=intent.operation_id)
        self.blocked(Code.OP_CONFLICT, lambda: self.store.prepare(conflict))

    def test_unresolved_blocks_same_loan_and_item(self):
        intent = plan(fixtures.loan(), fixtures.event(Action.APPROVE), fixtures.stock())
        self.store.prepare(intent)
        later = plan(fixtures.loan(), fixtures.event(Action.REJECT), fixtures.stock())
        self.blocked(Code.UNKNOWN, lambda: self.store.prepare(later))
        stage = StageRequest(stage_operation_id(fixtures.loan(), Action.APPROVE),
                             fixtures.loan(), Action.APPROVE, fixtures.MANAGER)
        self.blocked(Code.UNKNOWN, lambda: self.store.prepare(stage))

    def test_verified_receipt_allows_next_operation(self):
        intent = plan(fixtures.loan(), fixtures.event(Action.APPROVE), fixtures.stock())
        self.store.prepare(intent)
        self.store.save_receipt(Receipt(intent.operation_id, Outcome.UNKNOWN))
        restarted = FileJournal(self.root)
        loaded, receipt = restarted.load(intent.operation_id)
        self.assertEqual(loaded, intent)
        self.assertEqual(receipt.outcome, Outcome.UNKNOWN)
        restarted.save_receipt(Receipt(intent.operation_id, Outcome.VERIFIED,
                                       'synthetic-readback', intent.after, intent.stock_after))
        later = replace(
            fixtures.event(Action.RESERVE), actor=None, evidence_kind='system')
        next_intent = plan(intent.after, later, intent.stock_after)
        FileJournal(self.root).prepare(next_intent)

    def test_missing_load_is_keyerror(self):
        with self.assertRaises(KeyError):
            self.store.load('missing-operation')

    def test_stage_receipt_round_trip(self):
        request = StageRequest(stage_operation_id(fixtures.loan(), Action.APPROVE),
                               fixtures.loan(), Action.APPROVE, fixtures.MANAGER)
        self.store.prepare(request)
        receipt = StageReceipt(request.operation_id, Outcome.VERIFIED, fixtures.FORM,
                               None, 'synthetic-create', 'synthetic-readback')
        self.store.save_receipt(receipt)
        loaded, saved = FileJournal(self.root).load(request.operation_id)
        self.assertEqual(loaded, request)
        self.assertEqual(saved, receipt)
        self.assertEqual(FileJournal(self.root).unresolved_ids(), ())


if __name__ == '__main__':
    unittest.main()
