"""L1/L2 reminder wiring over synthetic ports; no live DingTalk, no network."""
import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap import remind
from bootstrap.journal import FileJournal
from contracts.flow import Action, Outcome, Receipt, plan
from contracts.model import Code, ContractError, Resource, State
from scheduler.reminder import BEFORE_DUE, OVERDUE, Reminder
from scheduler.runner import FileDedupStore, ReminderRunner

TZ = timezone(timedelta(hours=8))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / 'contracts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = _load('t07_remind_fixtures', 'fixtures.py')


def borrowed(due_at, borrower=None, quantity=2):
    loan = fixtures.loan()
    return replace(loan, state=State.BORROWED, due_at=due_at,
                   borrower=borrower or loan.borrower, quantity=quantity)


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def exchange(self, command, arguments):
        self.calls.append((command, dict(arguments)))
        return self.payload


class StubAdapter:
    def __init__(self, loans, broken=()):
        self.loans = {loan.ref.resource_id: loan for loan in loans}
        self.broken = set(broken)

    def read_loan(self, ref):
        if ref.resource_id in self.broken:
            raise ContractError(Code.WRONG_LOAN)
        return self.loans[ref.resource_id]


class FixedClock:
    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment


class ParseRefTests(unittest.TestCase):
    def test_reads_three_part_target(self):
        ref = remind.parse_ref('org/synthetic-loans/loan_x')
        self.assertEqual(ref, Resource('record', 'org', 'synthetic-loans', 'loan_x'))

    def test_reads_four_part_aitable_container(self):
        ref = remind.parse_ref('org/base/table/loan_x')
        self.assertEqual(ref, Resource('record', 'org', 'base/table', 'loan_x'))

    def test_rejects_three_part_when_container_must_be_two_segments(self):
        with self.assertRaises(ContractError):
            remind.parse_ref('org/base/loan_x', expected_container='base/table')

    def test_rejects_other_shapes(self):
        for raw in ('', 'org/base', 'org/base/x/y/z', 'org//x'):
            with self.assertRaises(ContractError):
                remind.parse_ref(raw)


class SourceTests(unittest.TestCase):
    def test_read_loans_reports_unreadable_candidates(self):
        loan = borrowed(datetime(2030, 1, 2, 17, tzinfo=TZ))
        adapter = StubAdapter([loan], broken=(loan.ref.resource_id,))
        loans, skipped = remind.read_loans(adapter, [loan.ref])
        self.assertEqual(loans, [])
        self.assertEqual(skipped[0][0], loan.ref)
        self.assertIn('ContractError', skipped[0][1])

    def test_sources_refs_reads_loan_refs_from_the_operator_list(self):
        loan = borrowed(datetime(2030, 1, 2, 17, tzinfo=TZ))
        raw = {'kind': 'record', 'tenant_id': loan.ref.tenant_id,
               'container_id': loan.ref.container_id, 'resource_id': loan.ref.resource_id}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sources.json'
            path.write_text(json.dumps([{'kind': 'event', 'loan': raw},
                                        {'loan': {'kind': 'form'}}]), encoding='utf-8')
            self.assertEqual(remind.sources_refs(path), [loan.ref])
            self.assertEqual(remind.sources_refs(Path(tmp) / 'missing.json'), [])

    def test_journal_refs_collect_loan_refs_once(self):
        loan = borrowed(datetime(2030, 1, 2, 17, tzinfo=TZ))
        intent = plan(fixtures.loan(), fixtures.event(Action.APPROVE), fixtures.stock())
        with tempfile.TemporaryDirectory() as tmp:
            journal = FileJournal(Path(tmp))
            journal.prepare(intent)
            journal.save_receipt(Receipt(intent.operation_id, Outcome.VERIFIED,
                                         'synthetic-readback', intent.after,
                                         intent.stock_after))
            refs = remind.journal_refs(journal)
        self.assertEqual(refs, [loan.ref])


class ReceiptTests(unittest.TestCase):
    def test_settled_and_unknown_receipts_stay_distinguishable(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipts = remind.Receipts(Path(tmp) / 'receipts.json')
            self.assertEqual(receipts.query('unknown-key'), 'unknown')
            self.assertTrue(receipts.record('sent-key', {'success': True}))
            self.assertEqual(receipts.query('sent-key'), 'ok')
            self.assertFalse(receipts.record('no-envelope-key', None))
            self.assertEqual(receipts.query('no-envelope-key'), 'unknown')
            self.assertFalse(receipts.record('rejected-key',
                                             {'success': False, 'error': {'code': 'X', 'message': 'y'}}))
            self.assertEqual(receipts.entries['rejected-key']['error'], 'X: y')
            reread = remind.Receipts(Path(tmp) / 'receipts.json')
            self.assertEqual(reread.query('sent-key'), 'ok')


class SenderTests(unittest.TestCase):
    def test_chat_sender_uses_borrower_and_reports_ok(self):
        loan = borrowed(datetime(2030, 1, 2, 17, tzinfo=TZ))
        reminder = Reminder('k', loan.ref, loan.borrower, BEFORE_DUE, '2030-01-01')
        transport = FakeTransport({'success': True})
        with tempfile.TemporaryDirectory() as tmp:
            sender = remind.ChatSender(transport, remind.Receipts(Path(tmp) / 'r.json'),
                                       remind.make_describe([loan], TZ))
            self.assertEqual(sender.send(reminder), 'ok')
        command, arguments = transport.calls[0]
        self.assertEqual(command, 'chat.send')
        self.assertEqual(arguments['user'], loan.borrower.user_id)
        self.assertEqual(arguments['title'], '工器具归还提醒')
        self.assertIn('2030-01-02 17:00', arguments['text'])
        self.assertIn('请在到期前归还', arguments['text'])

    def test_chat_sender_without_envelope_is_unknown(self):
        loan = borrowed(datetime(2030, 1, 2, 17, tzinfo=TZ))
        reminder = Reminder('k', loan.ref, loan.borrower, OVERDUE, '2030-01-03')
        with tempfile.TemporaryDirectory() as tmp:
            sender = remind.ChatSender(FakeTransport(None),
                                       remind.Receipts(Path(tmp) / 'r.json'),
                                       remind.make_describe([loan], TZ))
            self.assertEqual(sender.send(reminder), 'unknown')

    def test_overdue_text_says_overdue(self):
        loan = borrowed(datetime(2030, 1, 2, 17, tzinfo=TZ))
        reminder = Reminder('k', loan.ref, loan.borrower, OVERDUE, '2030-01-03')
        title, text = remind.make_describe([loan], TZ)(reminder)
        self.assertEqual(title, '工器具归还逾期提醒')
        self.assertIn('已逾期', text)

    def test_due_day_text_says_due_today(self):
        """Once the scheduler also fires on the due day, the copy must not say 'overdue'."""
        loan = borrowed(datetime(2030, 1, 2, 17, tzinfo=TZ))
        reminder = Reminder('k', loan.ref, loan.borrower, OVERDUE, '2030-01-02')
        title, text = remind.make_describe([loan], TZ)(reminder)
        self.assertEqual(title, '工器具归还到期提醒')
        self.assertIn('今天到期', text)
        self.assertNotIn('已逾期', text)


class RunnerWiringTests(unittest.TestCase):
    def test_one_message_then_dedup_on_second_pass(self):
        due = datetime(2030, 1, 2, 17, tzinfo=TZ)
        loan = borrowed(due)
        transport = FakeTransport({'success': True})
        with tempfile.TemporaryDirectory() as tmp:
            receipts = remind.Receipts(Path(tmp) / 'receipts.json')
            runner = ReminderRunner(FixedClock(datetime(2030, 1, 1, 10, tzinfo=TZ)),
                                    remind.ChatSender(transport, receipts,
                                                      remind.make_describe([loan], TZ)),
                                    receipts, FileDedupStore(Path(tmp) / 'reminders.json'))
            runner.start()
            first = runner.run([loan])
            runner.stop()
        self.assertTrue(first.window_open)
        self.assertEqual(len(first.issued), 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(first.issued[0].kind, BEFORE_DUE)
        self.assertEqual(runner.store.pending_keys(), [])
        second = runner.run([loan])
        self.assertEqual(second.issued, ())
        self.assertEqual(len(transport.calls), 1)

    def test_outside_window_sends_nothing(self):
        loan = borrowed(datetime(2030, 1, 2, 17, tzinfo=TZ))
        transport = FakeTransport({'success': True})
        with tempfile.TemporaryDirectory() as tmp:
            receipts = remind.Receipts(Path(tmp) / 'receipts.json')
            runner = ReminderRunner(FixedClock(datetime(2030, 1, 1, 19, tzinfo=TZ)),
                                    remind.ChatSender(transport, receipts,
                                                      remind.make_describe([loan], TZ)),
                                    receipts, FileDedupStore(Path(tmp) / 'reminders.json'))
            report = runner.run([loan])
        self.assertFalse(report.window_open)
        self.assertEqual(transport.calls, [])


class DedupeTests(unittest.TestCase):
    def test_repeated_source_entries_collapse_to_one_candidate(self):
        first = borrowed(datetime(2030, 1, 2, 17, tzinfo=TZ))
        second = replace(first, ref=replace(first.ref, resource_id='other'))
        self.assertEqual(remind.dedupe_refs([first.ref, first.ref, second.ref]),
                         [first.ref, second.ref])
        self.assertEqual(remind.dedupe_loans([first, replace(first), second]),
                         [first, second])


class NowFlagTests(unittest.TestCase):
    def test_parse_now_is_shanghai_time(self):
        stamp = remind.parse_now('2026-09-17 09:30')
        self.assertEqual((stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute),
                         (2026, 9, 17, 9, 30))
        self.assertEqual(stamp.utcoffset(), timedelta(hours=8))

    def test_parse_now_rejects_other_shapes(self):
        for raw in ('', '2026-09-17', '2026/09/17 09:30', 'tomorrow'):
            with self.assertRaises(ContractError):
                remind.parse_now(raw)

    def test_time_travel_needs_dry_run(self):
        with self.assertRaises(ContractError):
            remind.main(['--now', '2026-09-17 09:30'])


if __name__ == '__main__':
    unittest.main()
