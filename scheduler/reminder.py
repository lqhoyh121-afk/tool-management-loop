"""Reminder evaluation over frozen contracts; pure functions, no sending."""
from dataclasses import dataclass
from datetime import timedelta

from contracts.model import Resource, State, business_date

BEFORE_DUE = 'before_due'
OVERDUE = 'overdue'


@dataclass(frozen=True)
class Reminder:
    key: str
    loan_ref: Resource
    borrower: object
    kind: str
    send_day: str


def loan_prefix(loan):
    """Stable per-loan prefix shared by all of this loan's reminder keys."""
    return f'{loan.ref.tenant_id}::{loan.ref.container_id}::{loan.ref.resource_id}::'


def reminder_key(loan, kind, day):
    """Dedup key: loan + kind + business day (frozen D2/D3 semantics)."""
    return loan_prefix(loan) + f'{kind}::{day.isoformat()}'


def evaluate(loan, now):
    """Reminders due for one loan at now.

    Only borrowed loans are reminded (C1). Awaiting return confirmation
    (which is also how an unresolved return write presents) pauses (C4/C5).
    The day before the due business date sends before_due once (C2, D3);
    from the business day after the due date each business day sends one
    overdue reminder (C3, D3). Cross-day downtime collapses into a single
    reminder keyed by the current send day (C9/D2).
    """
    if loan.state != State.BORROWED:
        return []
    today = business_date(now)
    due_day = business_date(loan.due_at)
    if today == due_day - timedelta(days=1):
        kind, day = BEFORE_DUE, due_day
    elif today > due_day:
        kind, day = OVERDUE, today
    else:
        return []
    return [Reminder(reminder_key(loan, kind, day), loan.ref, loan.borrower, kind,
                     today.isoformat())]
