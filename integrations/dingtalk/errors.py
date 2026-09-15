"""解析层失败信号。

这些异常只服务本适配器的报文解析，不是公共业务错误码。连接层捕获后映射为
`contracts.model.ContractError`。
"""


class DingTalkShapeError(Exception):
    """读取传输报文时的失败基类。"""


class UnsupportedShapeError(DingTalkShapeError):
    """报文形态不在公开 T01 报告已观察范围内，拒绝猜测。"""


class MissingFieldError(DingTalkShapeError):
    """报文形态可识别，但缺少调用方点名的关键字段。"""


class BusinessErrorResponse(DingTalkShapeError):
    """传输层称成功，报文却带业务错误。

    `code` 与 `message` 原样取自报文，不在本层另编错误码。
    """

    def __init__(self, code, message=None):
        text = f'业务错误 {code}: {message}' if message else f'业务错误 {code}'
        super().__init__(text)
        self.code = code
        self.message = message


class UnknownResultError(DingTalkShapeError):
    """没有可用报文（如超时），结果未知。

    调用方必须先按精确目标回查再决定后续，禁止盲目重发。连接层把本异常映射为
    unknown 回执，不自动重发。
    """


class IdentityNamespaceError(DingTalkShapeError):
    """人员标识被跨命名空间使用。"""
