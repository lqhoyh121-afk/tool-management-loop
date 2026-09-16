"""待办详情与完成证据。

公开 T01 报告观察到：``todo task get`` 返回 ``result.todoDetailModel``；
``executorIds`` 与 ``activities[].creatorId`` 是待办内部人员 ID；实际完成事件的
``action`` 为 ``task.self.done`` 或 ``task.done``，带 ``activityId`` 与
``creatorId``，完成时间在 ``finishTime``。

主控对原始回执的类型核对：完成活动 ``creatorId`` 是 int，``finishTime`` 是 int；
未完成时 ``finishTime`` 为 0（见 ``docs/evidence/t01-trusted-application.md``）。
T07 真机核对：``finishTime`` 单位为毫秒；``completion_at`` 在连接层唯一换算为
Asia/Shanghai 带时区 datetime。``finish_time`` 仍原样返回整数，供形态校验与原始值保留。

报告同时记录：完成事件里没有明确审批结果字段，"勾完成"不等于"同意"；实际
完成者不能只依据 ``isDone`` 或 ``modifierId`` 判定。
"""
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from .envelope import read_todo_envelope
from .errors import MissingFieldError, UnsupportedShapeError
from .identity import TODO, PersonRef

DONE_ACTIONS = ('task.self.done', 'task.done')
# Asia/Shanghai civil time for modern dates (UTC+08, no tzdata dependency).
_SHANGHAI = timezone(timedelta(hours=8))


class CompletionEvent(NamedTuple):
    """一条实际完成活动。``actor`` 取自活动的 creatorId，不是执行人字段。"""

    activity_id: str
    action: str
    actor: PersonRef


def read_todo_detail(payload):
    envelope = read_todo_envelope(payload)
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
    """返回报文中的整数完成时间；未完成（0）时返回 None。

    不换算时区或纪元；单位由 T07 真机核对为毫秒，但本函数仍只返回原始整数。
    """
    if 'finishTime' not in detail:
        raise MissingFieldError('待办详情缺少 finishTime')
    value = detail['finishTime']
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedShapeError(
            f'finishTime 应为整数，收到 {type(value).__name__}'
        )
    if value < 0:
        raise UnsupportedShapeError(f'finishTime 不能为负数: {value!r}')
    if value == 0:
        return None
    return value


def completion_at(detail):
    """把 ``finishTime`` 毫秒换算为 Asia/Shanghai 带时区 datetime。

    这是连接层把待办完成时间写入 ``Event.occurred_at`` 的唯一入口；不读
    ``result.occurredAt``，也不在其他模块重复换算。
    """
    raw = finish_time(detail)
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(raw / 1000, tz=_SHANGHAI)
    except (OSError, OverflowError, ValueError) as exc:
        raise UnsupportedShapeError(
            f'finishTime 超出可换算范围: {raw!r}'
        ) from exc


def format_completion_display(when):
    """展示用 ``YYYY-MM-DD HH:mm``（Asia/Shanghai）；内部仍须保留带时区 datetime。"""
    local = when.astimezone(_SHANGHAI)
    return local.strftime('%Y-%m-%d %H:%M')


def _person_id(value, where):
    """待办内部人员 ID：观察类型为 int。

    转换规则：``bool`` 拒绝；``int`` 转为十进制规范字符串（``format(n, 'd')``）；
    字符串仅在等于该规范形式时接受，避免 ``"0123"`` 与 ``123`` 被当成同一人；
    其它类型拒绝。不做跨命名空间转换。
    """
    if value is None:
        raise MissingFieldError(f'{where} 缺少人员 ID')
    if isinstance(value, bool):
        raise UnsupportedShapeError(f'{where} 的人员 ID 不能是布尔值')
    if isinstance(value, int):
        return format(value, 'd')
    if isinstance(value, str):
        if not value:
            raise UnsupportedShapeError(f'{where} 的人员 ID 不能是空字符串')
        try:
            parsed = int(value, 10)
        except ValueError:
            raise UnsupportedShapeError(
                f'{where} 的人员 ID 字符串必须是十进制整数: {value!r}'
            ) from None
        canonical = format(parsed, 'd')
        if value != canonical:
            raise UnsupportedShapeError(
                f'{where} 的人员 ID 字符串必须是规范十进制 {canonical!r}，收到 {value!r}'
            )
        return canonical
    raise UnsupportedShapeError(
        f'{where} 的人员 ID 应为整数或规范十进制字符串，收到 {type(value).__name__}'
    )
