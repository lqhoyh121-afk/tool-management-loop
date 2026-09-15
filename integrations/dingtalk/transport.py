"""Injected transport only. No real DingTalk client or credentials."""
from .envelope import read_envelope
from .errors import UnknownResultError


class Transport:
    """Minimal exchange: command name plus JSON-like arguments.

    Returns a response envelope dict, or None when the result is unknown
    (timeout, dropped connection). Adapters must not retry a write on None.
    """

    def exchange(self, command, arguments):
        raise NotImplementedError


def require_envelope(payload):
    if payload is None:
        raise UnknownResultError('没有响应报文，结果未知，须按精确目标回查后再决定')
    return read_envelope(payload)
