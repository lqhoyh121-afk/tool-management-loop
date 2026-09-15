"""SYNTHETIC clock/sender/store; no real messages, no system time changes."""
import importlib.util
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contracts.model import State
from scheduler.reminder import BEFORE_DUE, OVERDUE, evaluate
from scheduler.runner import FileDedupStore, ReminderRunner

TZ = timezone(timedelta(hours=8))


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / 'contracts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = _load_module('sched_t02_fixtures', 'fixtures.py')


def at(day, hour, minute=0):
    return datetime(2030, 1, day, hour, minute, tzinfo=TZ)


def due(day, hour=12):
    return datetime(2030, 1, day, hour, tzinfo=TZ)


def borrowed(due_at, state=State.BORROWED):
    return replace(fixtures.loan(), state=state, due_at=due_at)


class FixedClock:
    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment


class ScriptedSender:
    def __init__(self, results=None):
        self.sent = []
        self.results = list(results or [])

    def send(self, reminder):
        self.sent.append(reminder)
        return self.results.pop(0) if self.results else 'ok'


class ScriptedQuery:
    def __init__(self, results=None):
        self.queries = []
        self.results = list(results or [])

    def query(self, key):
        self.queries.append(key)
        return self.results.pop(0) if self.results else 'unknown'


class ReminderTests(unittest.TestCase):
    def store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return FileDedupStore(Path(tmp.name) / 'dedup.json')

    def runner(self, moment, sender=None, query=None, store=None):
        return ReminderRunner(FixedClock(moment), sender or ScriptedSender(),
                              query or ScriptedQuery(), store or self.store())

    def kinds(self, report):
        return [reminder.kind for reminder in report.issued]

    def test_only_borrowed_state_is_reminded(self):
        now = at(4, 9, 30)
        for state in (State.AWAITING_APPROVAL, State.RESERVATION_PENDING,
                      State.AWAITING_ISSUE, State.AWAITING_RETURN, State.CLOSED,
                      State.REJECTED, State.CANCELLED):
            self.assertEqual(evaluate(borrowed(due(5), state), now), [])
        self.assertEqual(self.kinds(self.runner(now).run([borrowed(due(5))])),
                         [BEFORE_DUE])

    def test_one_day_early_once_per_day(self):
        store = self.store()
        report = self.runner(at(4, 9, 30), store=store).run([borrowed(due(5))])
        self.assertEqual(self.kinds(report), [BEFORE_DUE])
        self.assertTrue(report.issued[0].key.endswith('2030-01-05'))
        again = self.runner(at(4, 12, 0), store=store).run([borrowed(due(5))])
        self.assertEqual(again.issued, ())

    def test_due_day_is_not_overdue(self):
        report = self.runner(at(5, 9, 30)).run([borrowed(due(5))])
        self.assertEqual(report.issued, ())

    def test_overdue_once_per_business_day(self):
        store = self.store()
        first = self.runner(at(6, 9, 30), store=store).run([borrowed(due(5))])
        self.assertEqual(self.kinds(first), [OVERDUE])
        self.assertTrue(first.issued[0].key.endswith('2030-01-06'))
        same_day = self.runner(at(6, 15, 0), store=store).run([borrowed(due(5))])
        self.assertEqual(same_day.issued, ())
        next_day = self.runner(at(7, 9, 30), store=store).run([borrowed(due(5))])
        self.assertEqual(self.kinds(next_day), [OVERDUE])
        self.assertTrue(next_day.issued[0].key.endswith('2030-01-07'))

    def test_recipient_is_borrower_never_manager(self):
        runner = self.runner(at(4, 9, 30))
        report = runner.run([borrowed(due(5))])
        self.assertEqual(len(report.issued), 1)
        self.assertEqual(report.issued[0].borrower, fixtures.BORROWER)
        self.assertNotEqual(report.issued[0].borrower, fixtures.MANAGER)

    def test_before_trigger_time_nothing_sent(self):
        report = self.runner(at(4, 9, 29)).run([borrowed(due(5))])
        self.assertFalse(report.window_open)
        self.assertEqual(report.issued, ())

    def test_late_start_same_day_catches_up(self):
        report = self.runner(at(4, 10, 5)).run([borrowed(due(5))])
        self.assertEqual(self.kinds(report), [BEFORE_DUE])

    def test_after_six_pm_no_catchup(self):
        report = self.runner(at(4, 18, 1)).run([borrowed(due(5))])
        self.assertFalse(report.window_open)
        self.assertEqual(report.issued, ())
        edge = self.runner(at(4, 18, 0)).run([borrowed(due(5))])
        self.assertTrue(edge.window_open)

    def test_cross_day_downtime_merges_into_one(self):
        store = self.store()
        first = ReminderRunner(FixedClock(at(6, 9, 30)), ScriptedSender(),
                               ScriptedQuery(), store)
        self.assertEqual(self.kinds(first.run([borrowed(due(5))])), [OVERDUE])
        later = ReminderRunner(FixedClock(at(8, 9, 30)), ScriptedSender(),
                               ScriptedQuery(), store)
        report = later.run([borrowed(due(5))])
        self.assertEqual(self.kinds(report), [OVERDUE])
        self.assertEqual(len(report.issued), 1)
        self.assertTrue(report.issued[0].key.endswith('2030-01-08'))

    def test_unknown_send_requeries_original_key(self):
        sender = ScriptedSender(['unknown'])
        query = ScriptedQuery(['ok'])
        store = self.store()
        first = ReminderRunner(FixedClock(at(6, 9, 30)), sender, query, store)
        first.run([borrowed(due(5))])
        self.assertEqual(len(sender.sent), 1)
        second = ReminderRunner(FixedClock(at(6, 12, 0)), sender, query, store)
        report = second.run([borrowed(due(5))])
        self.assertEqual(report.issued, ())
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(len(query.queries), 1)

    def test_unknown_confirmed_not_sent_resends_once(self):
        sender = ScriptedSender(['unknown', 'ok'])
        query = ScriptedQuery(['not_sent'])
        store = self.store()
        first = ReminderRunner(FixedClock(at(6, 9, 30)), sender, query, store)
        first.run([borrowed(due(5))])
        second = ReminderRunner(FixedClock(at(6, 12, 0)), sender, query, store)
        report = second.run([borrowed(due(5))])
        self.assertEqual(self.kinds(report), [OVERDUE])
        self.assertEqual(len(sender.sent), 2)

    def test_unresolved_unknown_blocks_loan(self):
        sender = ScriptedSender(['unknown'])
        query = ScriptedQuery(['unknown'])
        store = self.store()
        first = ReminderRunner(FixedClock(at(6, 9, 30)), sender, query, store)
        first.run([borrowed(due(5))])
        second = ReminderRunner(FixedClock(at(7, 9, 30)), sender, query, store)
        report = second.run([borrowed(due(5))])
        self.assertEqual(report.issued, ())
        self.assertEqual(report.blocked_loans, 1)
        self.assertEqual(len(sender.sent), 1)

    def test_loans_do_not_cross_talk(self):
        other = replace(borrowed(due(5)),
                        ref=type(fixtures.LOAN)('record', 'synthetic-org',
                                                'synthetic-loans', 'loan-2'))
        report = self.runner(at(4, 9, 30)).run([borrowed(due(5)), other])
        self.assertEqual(len(report.issued), 2)
        keys = {reminder.key for reminder in report.issued}
        self.assertEqual(len(keys), 2)

    def test_restart_with_durable_store_does_not_resend(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'dedup.json'
        store = FileDedupStore(path)
        first = ReminderRunner(FixedClock(at(6, 9, 30)), ScriptedSender(),
                               ScriptedQuery(), store)
        first.start()
        self.assertEqual(self.kinds(first.run([borrowed(due(5))])), [OVERDUE])
        first.stop()

        reopened = FileDedupStore(path)
        self.assertEqual(reopened.status[0]['event'], 'started')
        self.assertEqual(reopened.status[-1]['event'], 'stopped')
        second = ReminderRunner(FixedClock(at(6, 16, 0)), ScriptedSender(),
                                ScriptedQuery(), reopened)
        report = second.run([borrowed(due(5))])
        self.assertEqual(report.issued, ())

    def test_business_date_uses_shanghai_day(self):
        utc_clock = FixedClock(datetime(2030, 1, 4, 1, 45, tzinfo=timezone.utc))
        report = ReminderRunner(utc_clock, ScriptedSender(), ScriptedQuery(),
                                self.store()).run([borrowed(due(5))])
        self.assertEqual(self.kinds(report), [BEFORE_DUE])
        early_utc = FixedClock(datetime(2030, 1, 4, 1, 29, tzinfo=timezone.utc))
        report = ReminderRunner(early_utc, ScriptedSender(), ScriptedQuery(),
                                self.store()).run([borrowed(due(5))])
        self.assertFalse(report.window_open)


if __name__ == '__main__':
    unittest.main()
