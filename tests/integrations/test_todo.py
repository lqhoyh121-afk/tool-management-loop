import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_fixture_loader import sample

from integrations.dingtalk.errors import (
    IdentityNamespaceError,
    MissingFieldError,
    UnknownResultError,
    UnsupportedShapeError,
)
from integrations.dingtalk.identity import CONTACT, TODO, PersonRef
from integrations.dingtalk.todo import (
    completed_by,
    completion_at,
    completion_events,
    executor_refs,
    finish_time,
    format_completion_display,
    read_todo_detail,
)

EXECUTOR = PersonRef(TODO, '9000000002')
CREATOR = PersonRef(TODO, '9000000001')
MANAGER = PersonRef(TODO, '9000000003')


class DetailReadTests(unittest.TestCase):
    def test_reads_detail_model(self):
        detail = read_todo_detail(sample('todo_detail_open'))
        self.assertEqual(detail['taskId'], 'SYNTHETIC-task-0001')

    def test_timeout_is_unknown_result(self):
        with self.assertRaises(UnknownResultError):
            read_todo_detail(None)

    def test_missing_detail_model_is_reported(self):
        payload = sample('todo_detail_open')
        del payload['result']['todoDetailModel']
        with self.assertRaises(MissingFieldError):
            read_todo_detail(payload)

    def test_executors_are_todo_namespace(self):
        detail = read_todo_detail(sample('todo_detail_open'))
        self.assertEqual(executor_refs(detail), [MANAGER])

    def test_missing_executors_is_reported(self):
        detail = read_todo_detail(sample('todo_detail_open'))
        del detail['executorIds']
        with self.assertRaises(MissingFieldError):
            executor_refs(detail)

    def test_finish_time_absent_while_open(self):
        self.assertIsNone(finish_time(read_todo_detail(sample('todo_detail_open'))))

    def test_finish_time_keeps_observed_int(self):
        self.assertEqual(
            finish_time(read_todo_detail(sample('todo_detail_done_cross_creator'))),
            9000000000001,
        )

    def test_finish_time_rejects_iso_string(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        detail['finishTime'] = '2026-09-14T17:30:00+08:00'
        with self.assertRaises(UnsupportedShapeError):
            finish_time(detail)

    def test_finish_time_rejects_bool(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        detail['finishTime'] = True
        with self.assertRaises(UnsupportedShapeError):
            finish_time(detail)

    def test_completion_at_converts_finish_time_milliseconds(self):
        expected = datetime(2026, 9, 14, 18, 0, tzinfo=timezone(timedelta(hours=8)))
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        detail['finishTime'] = int(expected.timestamp() * 1000)
        self.assertEqual(completion_at(detail), expected)

    def test_completion_at_absent_when_finish_time_zero(self):
        self.assertIsNone(completion_at(read_todo_detail(sample('todo_detail_open'))))

    def test_format_completion_display_shanghai(self):
        when = datetime(2026, 9, 14, 18, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(format_completion_display(when), '2026-09-14 18:00')

    def test_person_id_int_canonicalizes_and_rejects_bool(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        detail['executorIds'] = [True]
        with self.assertRaises(UnsupportedShapeError):
            executor_refs(detail)
        detail['executorIds'] = ['0123']
        with self.assertRaises(UnsupportedShapeError):
            executor_refs(detail)
        detail['executorIds'] = [9000000002]
        self.assertEqual(executor_refs(detail), [EXECUTOR])
        detail['executorIds'] = ['9000000002']
        self.assertEqual(executor_refs(detail), [EXECUTOR])


class CompletionEvidenceTests(unittest.TestCase):
    def test_only_done_actions_count(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        actions = [event.action for event in completion_events(detail)]
        self.assertEqual(actions, ['task.self.done', 'task.done'])

    def test_actor_comes_from_activity_creator_not_task_creator(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        actors = {event.actor for event in completion_events(detail)}
        self.assertEqual(actors, {EXECUTOR})
        self.assertTrue(completed_by(detail, EXECUTOR))
        self.assertFalse(completed_by(detail, CREATOR))

    def test_is_done_and_modifier_alone_are_not_evidence(self):
        detail = read_todo_detail(sample('todo_detail_done_without_activity'))
        self.assertTrue(detail['isDone'])
        self.assertEqual(detail['modifierId'], 9000000003)
        self.assertEqual(completion_events(detail), [])
        self.assertFalse(completed_by(detail, MANAGER))

    def test_completion_activity_without_id_is_reported(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        del detail['activities'][0]['activityId']
        with self.assertRaises(MissingFieldError):
            completion_events(detail)

    def test_completion_activity_without_creator_is_reported(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        del detail['activities'][0]['creatorId']
        with self.assertRaises(MissingFieldError):
            completion_events(detail)

    def test_activities_of_wrong_type_are_refused(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        detail['activities'] = 'task.done'
        with self.assertRaises(UnsupportedShapeError):
            completion_events(detail)

    def test_missing_activities_is_reported(self):
        detail = read_todo_detail(sample('todo_detail_open'))
        del detail['activities']
        with self.assertRaises(MissingFieldError):
            completion_events(detail)


class NamespaceTests(unittest.TestCase):
    def test_contact_id_cannot_answer_who_completed(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        contact = PersonRef(CONTACT, '9000000002')
        with self.assertRaises(IdentityNamespaceError):
            completed_by(detail, contact)

    def test_plain_string_is_not_a_person(self):
        detail = read_todo_detail(sample('todo_detail_done_cross_creator'))
        with self.assertRaises(UnsupportedShapeError):
            completed_by(detail, '9000000002')


if __name__ == '__main__':
    unittest.main()
