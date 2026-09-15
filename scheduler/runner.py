"""Reminder pass: window, dedup and unknown-send requery; no real sending."""
import json
from dataclasses import dataclass
from datetime import time, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .reminder import evaluate, loan_prefix

TRIGGER_AT = time(9, 30)      # D1: Asia/Shanghai daily trigger moment
CATCHUP_UNTIL = time(18, 0)   # D4: last catch-up moment; later startup defers
LOCAL_TZ = timezone(timedelta(hours=8))


class Clock(Protocol):
    def now(self):
        """Aware datetime; injected so tests never touch the system clock."""


class Sender(Protocol):
    def send(self, reminder):
        """'ok' or 'unknown'; write timeout is unknown, never auto-retried."""


class QueryPort(Protocol):
    def query(self, key):
        """'ok', 'not_sent' or 'unknown' for a previously sent key."""


class DedupStore(Protocol):
    def is_sent(self, key) -> bool: ...
    def mark_sent(self, key) -> None: ...
    def pending_keys(self): ...
    def mark_pending(self, key) -> None: ...
    def resolve_pending(self, key, delivered) -> None: ...
    def log_status(self, event, moment) -> None: ...


@dataclass(frozen=True)
class RunReport:
    started_at: str
    issued: tuple
    window_open: bool
    blocked_loans: int


class ReminderRunner:
    """One reminder pass per invocation; durable dedup survives restarts."""

    def __init__(self, clock, sender, query, store):
        self.clock = clock
        self.sender = sender
        self.query = query
        self.store = store

    def start(self):
        self.store.log_status('started', iso(self.clock.now()))

    def stop(self):
        self.store.log_status('stopped', iso(self.clock.now()))

    def run(self, loans):
        """Send what is due inside the window; returns the run report."""
        now = self.clock.now()
        local = now.astimezone(LOCAL_TZ)
        window_open = TRIGGER_AT <= local.time() <= CATCHUP_UNTIL
        if not window_open:
            return RunReport(iso(now), (), False, 0)
        self._resolve_pending()
        issued = []
        blocked = 0
        pending = list(self.store.pending_keys())
        for loan in loans:
            if any(key.startswith(loan_prefix(loan)) for key in pending):
                blocked += 1
                continue
            for reminder in evaluate(loan, now):
                if self.store.is_sent(reminder.key):
                    continue
                result = self.sender.send(reminder)
                if result == 'ok':
                    self.store.mark_sent(reminder.key)
                    issued.append(reminder)
                else:
                    self.store.mark_pending(reminder.key)
                    pending.append(reminder.key)
        return RunReport(iso(now), tuple(issued), True, blocked)

    def _resolve_pending(self):
        for key in list(self.store.pending_keys()):
            result = self.query.query(key)
            if result == 'ok':
                self.store.resolve_pending(key, True)
            elif result == 'not_sent':
                self.store.resolve_pending(key, False)


def iso(moment):
    return moment.isoformat()


class FileDedupStore:
    """JSON-file dedup and status journal: a local running record, not a ledger."""

    def __init__(self, path):
        self.path = Path(path)
        self.sent = set()
        self.pending = set()
        self.status = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.sent = set(data.get('sent', []))
            self.pending = set(data.get('pending', []))
            self.status = list(data.get('status', []))

    def is_sent(self, key):
        return key in self.sent

    def mark_sent(self, key):
        self.sent.add(key)
        self._flush()

    def pending_keys(self):
        return sorted(self.pending)

    def mark_pending(self, key):
        self.pending.add(key)
        self._flush()

    def resolve_pending(self, key, delivered):
        self.pending.discard(key)
        if delivered:
            self.sent.add(key)
        self._flush()

    def log_status(self, event, moment):
        self.status.append({'event': event, 'at': moment})
        self._flush()

    def _flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'sent': sorted(self.sent),
                                   'pending': sorted(self.pending),
                                   'status': self.status},
                                  ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(self.path)
