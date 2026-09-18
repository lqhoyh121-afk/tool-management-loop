"""报文封套与记录列表的读取。

依据公开 T01 报告及主控对原始回执的类型核对：

1. Base 不存在时出现顶层 ``success: true``，同时 ``status: "error"`` 且
   ``error.code=BASE_NOT_FOUND``。所以不能把 success 布尔值当成功判据。
2. 成功记录封套为 ``status: "success"`` 且 ``error: {}``。空对象不是缺 code。
3. ``record query`` 返回 ``data.records``；``record query --all`` 有数据时是顶层
   ``records``；空表曾返回 ``records: null, hasMore: false, pages: 1``。
   ``hasMore`` 必须是布尔 ``false``，缺字段或其它假值不能当空成功。

``errorCode`` / ``errorMsg`` 属于 todo 通道（T07 真机 #34 观测），aitable 通道仍拒绝。
"""
from .errors import (
    BusinessErrorResponse,
    MissingFieldError,
    UnknownResultError,
    UnsupportedShapeError,
)

_MISSING = object()
_UNSUPPORTED_ERROR_KEYS = ('errorCode', 'errorMsg')


def read_envelope(payload):
    """校验一份报文并原样返回。

    ``payload`` 为 None 表示没拿到响应（超时等），结果未知。
    """
    if payload is None:
        raise UnknownResultError('没有响应报文，结果未知，须按精确目标回查后再决定')
    if not isinstance(payload, dict):
        raise UnsupportedShapeError(f'报文顶层应为对象，收到 {type(payload).__name__}')

    extra = [key for key in _UNSUPPORTED_ERROR_KEYS if key in payload]
    if extra:
        raise UnsupportedShapeError(
            f'未支持的错误封套字段 {extra[0]!r}，公开报告未确认该形态'
        )

    if 'error' not in payload:
        raise UnsupportedShapeError('报文缺少 error 字段，不能把缺字段当成功')
    error = payload['error']
    status = payload.get('status', _MISSING)
    success = payload.get('success')

    if isinstance(error, dict):
        code = error.get('code')
        if code:
            raise BusinessErrorResponse(code, error.get('message'))
        if error:
            raise UnsupportedShapeError('报文带 error 对象但没有 code，无法判定结果')
    else:
        raise UnsupportedShapeError(f'error 字段应为对象，收到 {type(error).__name__}')

    if status is _MISSING:
        raise UnsupportedShapeError('报文缺少 status，不能把缺字段当成功')
    if status == 'error':
        raise UnsupportedShapeError('status=error 但没有 error.code，无法判定结果')
    if status != 'success':
        raise UnsupportedShapeError(f'未观察过的 status 取值: {status!r}')

    if 'success' not in payload:
        raise UnsupportedShapeError('报文缺少 success，不能把缺字段当成功')
    if success is False:
        raise UnsupportedShapeError('success=false 但没有 error.code，无法判定结果')
    if success is not True:
        raise UnsupportedShapeError(f'success 应为布尔 true，收到 {success!r}')

    return payload


_AITABLE_KEYS = ('status', 'error')


def read_todo_envelope(payload):
    """校验 todo 通道报文并原样返回。

    T07 真机观测成功形态：``success: true``、``errorCode``/``errorMsg`` 为 ``null``、
    顶层 ``result``，**无** ``status``/``error``。失败时 ``errorCode`` 为非空字符串。
    """
    if payload is None:
        raise UnknownResultError('没有响应报文，结果未知，须按精确目标回查后再决定')
    if not isinstance(payload, dict):
        raise UnsupportedShapeError(f'报文顶层应为对象，收到 {type(payload).__name__}')

    mixed = [key for key in _AITABLE_KEYS if key in payload]
    if mixed:
        raise UnsupportedShapeError(
            f'todo 报文不应含 aitable 字段 {mixed[0]!r}，须按通道分别读取'
        )

    if 'success' not in payload:
        raise UnsupportedShapeError('todo 报文缺少 success，不能把缺字段当成功')
    success = payload['success']
    if success is not True and success is not False:
        raise UnsupportedShapeError(f'success 应为布尔值，收到 {success!r}')

    if 'errorCode' not in payload or 'errorMsg' not in payload:
        raise UnsupportedShapeError('todo 报文缺少 errorCode 或 errorMsg')

    error_code = payload['errorCode']
    error_msg = payload['errorMsg']
    if error_code is not None:
        if not isinstance(error_code, str) or not error_code:
            raise UnsupportedShapeError('todo 报文 errorCode 应为非空字符串或 null')
        message = error_msg if isinstance(error_msg, str) else None
        raise BusinessErrorResponse(error_code, message)

    if success is False:
        raise UnsupportedShapeError('success=false 但 errorCode 为空，无法判定结果')

    if error_msg is not None and error_msg != '':
        raise UnsupportedShapeError('errorCode 为空时 errorMsg 必须为空或 null')

    return payload


def extract_records(payload):
    """取出记录列表，兼容单页与 ``--all`` 两种形态。"""
    envelope = read_envelope(payload)

    data = envelope.get('data')
    has_data = isinstance(data, dict) and 'records' in data
    has_top = 'records' in envelope
    if has_data and has_top:
        raise UnsupportedShapeError('data.records 与顶层 records 并存，形态歧义，不能静默取边')
    if has_data:
        container = data
    elif has_top:
        container = envelope
    elif isinstance(data, dict):
        raise UnsupportedShapeError('data 对象里没有 records，未观察过该形态')
    else:
        raise UnsupportedShapeError('报文既没有 data.records 也没有顶层 records')

    records = container.get('records')
    if records is None:
        # 空表的已知形态：records=null 且 hasMore 恰好为 false。
        if 'hasMore' not in container or container['hasMore'] is not False:
            raise UnsupportedShapeError(
                'records 为 null 时 hasMore 必须恰好是 false，不能当空结果'
            )
        return []
    if not isinstance(records, list):
        raise UnsupportedShapeError(f'records 应为列表或 null，收到 {type(records).__name__}')
    for item in records:
        if not isinstance(item, dict):
            raise UnsupportedShapeError(f'记录项应为对象，收到 {type(item).__name__}')
    return records


def record_id(record):
    value = record.get('recordId')
    if value is None:
        raise MissingFieldError('记录缺少 recordId')
    if not isinstance(value, str) or not value:
        raise UnsupportedShapeError('recordId 应为非空字符串')
    return value


def record_cells(record):
    cells = record.get('cells')
    if cells is None:
        raise MissingFieldError('记录缺少 cells')
    if not isinstance(cells, dict):
        raise UnsupportedShapeError(f'cells 应为对象，收到 {type(cells).__name__}')
    return cells


def query_rows(payload):
    """Rows from a ``record query --all`` reply, filtered or unfiltered.

    Live observation (T01/T07): the ``--all`` reply is not the single-record
    envelope — a filtered query has no ``success``/``status``/``error`` at all,
    and an empty result set is ``records: null`` (present key, null value), not
    ``[]``. A *missing* key stays an error: that is a shape change, not an empty
    set. Truncation fails closed instead of matching on an incomplete list.

    ``records: null`` is only an empty result when ``hasMore`` says exactly
    ``false``, the same rule :func:`extract_records` already applies: a null list
    with a missing or non-``false`` ``hasMore`` is a shape change (or a failed
    query answered with a null), never "no rows". Reading it as empty is how a
    permanent, silent "nobody applied today" would look.
    """
    if not isinstance(payload, dict):
        raise UnsupportedShapeError('record query 未返回对象报文')
    if payload.get('hasMore'):
        raise UnsupportedShapeError('record query 分页未拉完，拒绝按不完整结果匹配')
    if 'records' not in payload:
        raise UnsupportedShapeError('record query 报文缺少 records 键')
    records = payload['records']
    if records is None:
        # 空结果集的已知形态：records=null 且 hasMore 恰好为 false。
        if 'hasMore' not in payload or payload['hasMore'] is not False:
            raise UnsupportedShapeError(
                'record query 的 records 为 null 时 hasMore 必须恰好是 false，'
                '不能当空结果'
            )
        records = []
    elif not isinstance(records, list):
        raise UnsupportedShapeError('record query 的 records 不是数组')
    rows = []
    for item in records:
        if not isinstance(item, dict):
            raise UnsupportedShapeError('record query 的记录不是对象')
        row_id = item.get('recordId')
        cells = item.get('cells')
        if not isinstance(row_id, str) or not row_id.strip():
            raise UnsupportedShapeError('record query 的记录缺少 recordId')
        if not isinstance(cells, dict):
            raise UnsupportedShapeError('record query 的记录缺少 cells')
        rows.append({'recordId': row_id, 'cells': cells})
    return rows


def created_record_id(payload):
    """单个新建记录的 id（``data.newRecordIds``）。

    新建回执只用来定位刚写的行；真正的成功判据是随后的精确回读，不是这里。
    空数组、多元素或非字符串都按未观察形态 fail closed。
    """
    envelope = read_envelope(payload)
    data = envelope.get('data')
    if not isinstance(data, dict):
        raise UnsupportedShapeError('新建回执缺少 data 对象')
    ids = data.get('newRecordIds')
    if not isinstance(ids, list) or len(ids) != 1:
        raise UnsupportedShapeError('新建回执的 newRecordIds 不是单元素数组')
    record = ids[0]
    if not isinstance(record, str) or not record.strip():
        raise UnsupportedShapeError('新建回执的 newRecordIds 不是非空字符串')
    return record
