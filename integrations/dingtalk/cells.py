"""多维表单元格取值。

公开 T01 报告观察到的形态：``cells[fieldId]`` 下，creator 是
``[{corpId, userId}]``；number 本次回读为字符串，需要显式数值解析；date 是带时区
ISO 字符串；singleSelect 是 ``{id, name}``。

本模块只做取值和形态校验，不判定业务含义，也不给字段起公共业务名。
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from .errors import MissingFieldError, UnsupportedShapeError
from .identity import RECORD_CREATOR, PersonRef


class SelectOption(NamedTuple):
    """singleSelect 单元格原样返回的选项标识与文案。"""

    id: str
    name: str


def _raw(cells, field_id):
    if field_id not in cells:
        raise MissingFieldError(f'单元格缺少字段 {field_id}')
    value = cells[field_id]
    if value is None:
        raise MissingFieldError(f'字段 {field_id} 为空')
    return value


def read_text(cells, field_id):
    value = _raw(cells, field_id)
    if not isinstance(value, str):
        raise UnsupportedShapeError(f'字段 {field_id} 应为字符串，收到 {type(value).__name__}')
    return value


def read_text_or_empty(cells, field_id):
    """Optional text: missing or null is empty. Present non-strings still fail."""
    if field_id not in cells or cells[field_id] is None:
        return ''
    return read_text(cells, field_id)


def read_number(cells, field_id):
    """返回 :class:`~decimal.Decimal`。

    报告只观察到 number 回读为字符串。非字符串（含 int/float/bool）一律拒绝，
    不走旁路；解析后必须是有限数。
    """
    value = _raw(cells, field_id)
    if isinstance(value, bool):
        raise UnsupportedShapeError(f'字段 {field_id} 是布尔值，不是数量')
    if not isinstance(value, str):
        raise UnsupportedShapeError(
            f'字段 {field_id} 应为数字字符串，收到 {type(value).__name__}'
        )
    text = value.strip()
    if not text:
        raise MissingFieldError(f'字段 {field_id} 是空字符串，不能当 0')
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        raise UnsupportedShapeError(f'字段 {field_id} 不是可解析的数字: {text!r}') from None
    if not parsed.is_finite():
        raise UnsupportedShapeError(f'字段 {field_id} 不是有限数字: {text!r}')
    return parsed


def read_datetime(cells, field_id):
    """返回带时区的 :class:`~datetime.datetime`。

    报告只观察到带时区 ISO 字符串。无时区的取值会改变业务日期口径，这里拒绝而
    不是补一个默认时区。
    """
    value = _raw(cells, field_id)
    if not isinstance(value, str):
        raise UnsupportedShapeError(f'字段 {field_id} 应为 ISO 时间字符串，收到 {type(value).__name__}')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise UnsupportedShapeError(f'字段 {field_id} 不是可解析的 ISO 时间: {value!r}') from None
    if parsed.tzinfo is None:
        raise UnsupportedShapeError(f'字段 {field_id} 缺时区，不推断本地时区: {value!r}')
    return parsed


def read_single_select(cells, field_id):
    value = _raw(cells, field_id)
    if not isinstance(value, dict):
        raise UnsupportedShapeError(f'字段 {field_id} 应为 {{id, name}} 对象，收到 {type(value).__name__}')
    option_id = value.get('id')
    option_name = value.get('name')
    if not isinstance(option_id, str) or not option_id:
        raise MissingFieldError(f'字段 {field_id} 缺少选项 id')
    if not isinstance(option_name, str) or not option_name:
        raise MissingFieldError(f'字段 {field_id} 缺少选项 name')
    return SelectOption(option_id, option_name)


def read_creator(cells, field_id):
    """返回 ``record_creator`` 命名空间的 :class:`PersonRef`。

    姓名字符串不能代替系统 creator；报告明确记录 creator 是组织加人员的二元身份。
    """
    value = _raw(cells, field_id)
    if isinstance(value, str):
        raise UnsupportedShapeError(
            f'字段 {field_id} 是字符串，系统 creator 不能用姓名代替'
        )
    if not isinstance(value, list):
        raise UnsupportedShapeError(f'字段 {field_id} 应为 creator 列表，收到 {type(value).__name__}')
    if len(value) != 1:
        raise UnsupportedShapeError(
            f'字段 {field_id} 含 {len(value)} 个 creator，公开报告只观察到单个'
        )
    entry = value[0]
    if not isinstance(entry, dict):
        raise UnsupportedShapeError(f'字段 {field_id} 的 creator 项应为对象')
    user_id = entry.get('userId')
    corp_id = entry.get('corpId')
    if not isinstance(user_id, str) or not user_id:
        raise MissingFieldError(f'字段 {field_id} 的 creator 缺少 userId')
    if not isinstance(corp_id, str) or not corp_id:
        raise MissingFieldError(f'字段 {field_id} 的 creator 缺少 corpId')
    return PersonRef(RECORD_CREATOR, user_id, corp_id)
