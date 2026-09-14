"""报文封套与记录列表的读取。

依据公开 T01 报告的两处事实：

1. Base 不存在时出现顶层 ``success: true``，同时 ``status: "error"`` 且
   ``error.code=BASE_NOT_FOUND``。所以不能把 success 布尔值当成功判据。
2. ``record query`` 返回 ``data.records``；``record query --all`` 有数据时是顶层
   ``records``；空表曾返回 ``records: null, hasMore: false, pages: 1``。适配器要
   同时处理这两种形态，且 null 不能直接当异常。

报告未记录的形态一律报错，不猜。
"""
from .errors import (
    BusinessErrorResponse,
    MissingFieldError,
    UnknownResultError,
    UnsupportedShapeError,
)


def read_envelope(payload):
    """校验一份报文并原样返回。

    ``payload`` 为 None 表示没拿到响应（超时等），结果未知。
    """
    if payload is None:
        raise UnknownResultError('没有响应报文，结果未知，须按精确目标回查后再决定')
    if not isinstance(payload, dict):
        raise UnsupportedShapeError(f'报文顶层应为对象，收到 {type(payload).__name__}')

    error = payload.get('error')
    status = payload.get('status')
    success = payload.get('success')

    if isinstance(error, dict):
        code = error.get('code')
        if not code:
            raise UnsupportedShapeError('报文带 error 对象但没有 code，无法判定结果')
        raise BusinessErrorResponse(code, error.get('message'))
    if error is not None:
        raise UnsupportedShapeError(f'error 字段应为对象，收到 {type(error).__name__}')

    if status == 'error':
        raise UnsupportedShapeError('status=error 但没有 error.code，无法判定结果')
    if status is not None and status != 'ok':
        raise UnsupportedShapeError(f'未观察过的 status 取值: {status!r}')

    if success is False:
        raise UnsupportedShapeError('success=false 但没有 error.code，无法判定结果')
    if success is not None and success is not True:
        raise UnsupportedShapeError(f'success 应为布尔值，收到 {type(success).__name__}')

    return payload


def extract_records(payload):
    """取出记录列表，兼容单页与 ``--all`` 两种形态。"""
    envelope = read_envelope(payload)

    data = envelope.get('data')
    if isinstance(data, dict) and 'records' in data:
        container = data
    elif 'records' in envelope:
        container = envelope
    elif isinstance(data, dict):
        raise UnsupportedShapeError('data 对象里没有 records，未观察过该形态')
    else:
        raise UnsupportedShapeError('报文既没有 data.records 也没有顶层 records')

    records = container.get('records')
    if records is None:
        # 空表的已知形态：records=null 且 hasMore=false。
        if container.get('hasMore'):
            raise UnsupportedShapeError('records 为 null 但 hasMore 为真，不能当空结果')
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
