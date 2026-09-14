"""待办详情与完成证据。

公开 T01 报告观察到：``todo task get`` 返回 ``result.todoDetailModel``；
``executorIds`` 与 ``activities[].creatorId`` 是待办内部人员 ID；实际完成事件的
``action`` 为 ``task.self.done`` 或 ``task.done``，带 ``activityId`` 与
``creatorId``，完成时间在 ``finishTime``。报告同时记录：完成事件里没有明确审批
结果字段，"勾完成"不等于"同意"；实际完成者不能只依据 ``isDone`` 或
``modifierId`` 判定。
"""
from datetime import datetime
from typing import NamedTuple

from .envelope import read_envelope
from .errors import MissingFieldError, UnsupportedShapeError
from .identity import TODO, PersonRef

DONE_ACTIONS = ('task.self.done', 'task.done')


class CompletionEvent(NamedTuple):
    """一条实际完成活动。``actor`` 取自活动的 creatorId，不是执行人字段。"""

    activity_id: str
    action: str
    actor: PersonRef


def read_todo_detail(payload):
    envelope = read_envelope(payload)
    result = envelope.get('result')
    if result is None:
        raise MissingFieldError('待办报文缺少 result')
    if not isinstance(result, dict):
        raise UnsupportedShapeError(f'result 应为对象，收到 {type(result).__name__}')
    detail = result.get('todoDetailModel')
    if detail is None:
        raise MissingFieldError('待办报文缺少 result.todoDetailModel')
    if not isinstance(detail, dict):
        raise UnsupportedShapeError(f'todoDetailModel 应为对象，收到 {type(detail).__name__}')
    return detail


def executor_refs(detail):
    raw = detail.get('executorIds')
    if raw is None:
        raise MissingFieldError('待办详情缺少 executorIds')
    if not isinstance(raw, list):
        raise UnsupportedShapeError(f'executorIds 应为列表，收到 {type(raw).__name__}')
    return [PersonRef(TODO, _person_id(value, 'executorIds')) for value in raw]


def completion_events(detail):
    """只返回实际完成活动，顺序与报文一致。

    不看 ``isDone``，不看 ``modifierId``；报告已记录这两者不足以证明谁完成。
    """
    activities = detail.get('activities')
    if activities is None:
        raise MissingFieldError('待办详情缺少 activities')
    if not isinstance(activities, list):
        raise UnsupportedShapeError(f'activities 应为列表，收到 {type(activities).__name__}')

    events = []
    for item in activities:
        if not isinstance(item, dict):
            raise UnsupportedShapeError(f'活动项应为对象，收到 {type(item).__name__}')
        action = item.get('action')
        if action is None:
            raise MissingFieldError('活动项缺少 action')
        if not isinstance(action, str):
            raise UnsupportedShapeError(f'action 应为字符串，收到 {type(action).__name__}')
        if action not in DONE_ACTIONS:
            continue
        activity_id = item.get('activityId')
        if not isinstance(activity_id, str) or not activity_id:
            raise MissingFieldError('完成活动缺少 activityId')
        actor = PersonRef(TODO, _person_id(item.get('creatorId'), 'activities[].creatorId'))
        events.append(CompletionEvent(activity_id, action, actor))
    return events


def completed_by(detail, person):
    """``person`` 是否有实际完成活动。

    ``person`` 必须是待办内部命名空间的标识；传通讯录标识会报错，而不是静默
    比较两个不同命名空间的取值。
    """
    if not isinstance(person, PersonRef):
        raise UnsupportedShapeError('person 必须是 PersonRef')
    person.require(TODO)
    return any(event.actor.same_person_as(person) for event in completion_events(detail))


def finish_time(detail):
    """返回带时区的完成时间；未完成时返回 None。"""
    value = detail.get('finishTime')
    if value is None:
        return None
    if not isinstance(value, str):
        raise UnsupportedShapeError(f'finishTime 应为 ISO 时间字符串，收到 {type(value).__name__}')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise UnsupportedShapeError(f'finishTime 不是可解析的 ISO 时间: {value!r}') from None
    if parsed.tzinfo is None:
        raise UnsupportedShapeError(f'finishTime 缺时区，不推断本地时区: {value!r}')
    return parsed


def _person_id(value, where):
    if value is None:
        raise MissingFieldError(f'{where} 缺少人员 ID')
    if not isinstance(value, str) or not value:
        raise UnsupportedShapeError(f'{where} 的人员 ID 应为非空字符串')
    return value
