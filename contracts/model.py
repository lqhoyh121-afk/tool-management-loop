"""Normalized values, not raw DingTalk payloads. Adapters must prove evidence."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class Code(StrEnum):
    INVALID = "INVALID_INPUT"
    IDENTITY = "IDENTITY_REQUIRED"
    WRONG_PERSON = "WRONG_PERSON"
    WRONG_LOAN = "WRONG_LOAN"
    EVIDENCE = "EVIDENCE_REQUIRED"
    CONFIG = "CONFIG_RECONFIRM_REQUIRED"
    STATE = "INVALID_STATE"
    DUPLICATE = "DUPLICATE_EVENT"
    QUANTITY = "QUANTITY_MISMATCH"
    DUE = "DUE_AT_MISMATCH"
    IDENTIFIERS = "PHYSICAL_IDS_REQUIRED"
    CONFLICT = "RESERVATION_CONFLICT"
    UNKNOWN = "WRITE_UNKNOWN_QUERY_FIRST"
    READBACK = "READBACK_MISMATCH"
    OP_CONFLICT = "OPERATION_ID_CONFLICT"
    INSTANCE = "SECOND_INSTANCE_BLOCKED"


class ContractError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code.value)


def require(condition, code=Code.INVALID):
    if not condition:
        raise ContractError(code)


def text(value):
    require(isinstance(value, str) and bool(value.strip()) and value == value.strip())


def aware(value):
    require(isinstance(value, datetime) and value.utcoffset() is not None)


def business_date(value):
    aware(value)
    # Asia/Shanghai civil dates for this MVP (modern UTC+08, no tzdata dependency).
    require(value.year >= 2000)
    return value.astimezone(timezone(timedelta(hours=8))).date()


def quantity(value, zero=False):
    require(type(value) is int and value >= (0 if zero else 1), Code.QUANTITY)


class State(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    RESERVATION_PENDING = "reservation_pending"
    AWAITING_ISSUE = "awaiting_issue_confirmation"
    BORROWED = "borrowed"
    AWAITING_RETURN = "awaiting_return_confirmation"
    CLOSED = "closed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class Action(StrEnum):
    APPLY = "apply"
    APPROVE = "approve"
    REJECT = "reject"
    RESERVE = "reserve"
    ISSUE = "confirm_issue"
    REQUEST_RETURN = "request_return"
    RETURN = "confirm_return"
    CANCEL = "cancel"


class Outcome(StrEnum):
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"
    VERIFIED = "verified"
    NOT_APPLIED = "not_applied"


@dataclass(frozen=True)
class Identity:
    namespace: str
    tenant_id: str
    user_id: str

    def __post_init__(self):
        require(self.namespace in ("contact", "todo"), Code.IDENTITY)
        for value in (self.tenant_id, self.user_id):
            text(value)


@dataclass(frozen=True)
class Resource:
    kind: str
    tenant_id: str
    container_id: str
    resource_id: str

    def __post_init__(self):
        require(self.kind in ("record", "form", "todo"))
        for value in (self.tenant_id, self.container_id, self.resource_id):
            text(value)


@dataclass(frozen=True)
class IdentityBinding:
    contact: Identity
    internal: Identity
    task: Resource
    creation_evidence: str
    readback_evidence: str

    def __post_init__(self):
        require(self.contact.namespace == "contact" and self.internal.namespace == "todo",
                Code.IDENTITY)
        require(self.contact.tenant_id == self.internal.tenant_id == self.task.tenant_id,
                Code.IDENTITY)
        require(self.task.kind == "todo", Code.EVIDENCE)
        text(self.creation_evidence)
        text(self.readback_evidence)


@dataclass(frozen=True)
class Loan:
    ref: Resource
    item: Resource
    borrower: Identity
    approver: Identity
    manager: Identity
    quantity: int
    tracked: bool
    physical_ids: tuple[str, ...]
    due_at: datetime
    config_version: str
    state: State = State.AWAITING_APPROVAL
    return_ref: Resource | None = None
    consumed_events: tuple[str, ...] = ()
    application_evidence: str = ""

    def __post_init__(self):
        require(self.ref.kind == self.item.kind == "record")
        require(self.ref.tenant_id == self.item.tenant_id)
        for actor in (self.borrower, self.approver, self.manager):
            require(isinstance(actor, Identity) and actor.namespace == "contact", Code.IDENTITY)
            require(actor.tenant_id == self.ref.tenant_id, Code.IDENTITY)
        quantity(self.quantity)
        require(type(self.tracked) is bool and isinstance(self.state, State))
        require(type(self.physical_ids) is tuple, Code.IDENTIFIERS)
        require(len(set(self.physical_ids)) == len(self.physical_ids), Code.IDENTIFIERS)
        for identifier in self.physical_ids:
            text(identifier)
        require(len(self.physical_ids) == (self.quantity if self.tracked else 0), Code.IDENTIFIERS)
        aware(self.due_at)
        text(self.config_version)
        require(type(self.consumed_events) is tuple)
        require(isinstance(self.application_evidence, str))
        if self.return_ref is not None:
            require(self.return_ref.kind == "record"
                    and self.return_ref.tenant_id == self.ref.tenant_id, Code.WRONG_LOAN)


@dataclass(frozen=True)
class Inventory:
    ref: Resource
    available: int
    reserved: int
    borrowed: int
    available_ids: tuple[str, ...]
    reserved_ids: tuple[str, ...]
    borrowed_ids: tuple[str, ...]
    revision: str

    def __post_init__(self):
        require(self.ref.kind == "record")
        for count in (self.available, self.reserved, self.borrowed):
            quantity(count, zero=True)
        groups = (self.available_ids, self.reserved_ids, self.borrowed_ids)
        for group in groups:
            require(type(group) is tuple, Code.IDENTIFIERS)
            for identifier in group:
                text(identifier)
        all_ids = sum(groups, ())
        require(len(set(all_ids)) == len(all_ids), Code.IDENTIFIERS)
        text(self.revision)


@dataclass(frozen=True)
class Event:
    action: Action
    event_id: str
    loan_ref: Resource
    source: Resource
    actor: Identity | None
    occurred_at: datetime
    config_version: str
    evidence_kind: str
    verified: bool = False
    binding: IdentityBinding | None = None
    return_ref: Resource | None = None
    quantity: int | None = None
    physical_ids: tuple[str, ...] = ()
    evidence_ref: str = ""
    #: 申请行上的归还时间（申请入口的「归还时间」格）。表单没声明这一格时为 None，
    #: 受理侧据此不做比对 —— 声明了就必须与台账行一致，申请后改时间不算「同一笔申请」。
    due_at: datetime | None = None

    def __post_init__(self):
        require(isinstance(self.action, Action))
        text(self.event_id)
        text(self.config_version)
        aware(self.occurred_at)
        require(self.evidence_kind in ("form", "todo_completion", "system"))
        require(type(self.verified) is bool)
        require(isinstance(self.evidence_ref, str))
        if self.quantity is not None:
            quantity(self.quantity)
        if self.due_at is not None:
            aware(self.due_at)
