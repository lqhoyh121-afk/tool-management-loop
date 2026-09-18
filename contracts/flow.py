"""Pure transition plans. A plan is NOT permission to bypass adapter gates."""
from dataclasses import dataclass, replace
import json
from uuid import NAMESPACE_URL, uuid5

from .model import (Action, Code, Event, Inventory, Loan, Outcome, State,
                    require)


@dataclass(frozen=True)
class WriteIntent:
    operation_id: str
    before: Loan
    after: Loan
    stock_before: Inventory
    stock_after: Inventory
    event: Event


@dataclass(frozen=True)
class Receipt:
    operation_id: str
    outcome: Outcome
    readback_evidence: str = ""
    loan: Loan | None = None
    inventory: Inventory | None = None


@dataclass(frozen=True)
class Resolution:
    loan: Loan
    outcome: Outcome
    error: Code | None = None


def operation_id(loan, event):
    """Stable across restarts; never use scan time, names or random retry IDs."""
    parts = (loan.ref.tenant_id, loan.ref.container_id, loan.ref.resource_id,
             event.action.value, event.source.kind, event.source.container_id,
             event.source.resource_id, event.event_id)
    return str(uuid5(NAMESPACE_URL, json.dumps(parts, ensure_ascii=True)))


def check_event(loan: Loan, event: Event):
    require(event.loan_ref == loan.ref and event.source.tenant_id == loan.ref.tenant_id,
            Code.WRONG_LOAN)
    require(event.config_version == loan.config_version, Code.CONFIG)
    require(event.verified and bool(event.evidence_ref.strip()), Code.EVIDENCE)
    require(event.event_id not in loan.consumed_events, Code.DUPLICATE)
    if event.action == Action.RESERVE:
        require(event.evidence_kind == "system" and event.actor is None, Code.EVIDENCE)
        return
    require(event.actor is not None, Code.IDENTITY)
    expected = (loan.borrower if event.action in (Action.APPLY, Action.REQUEST_RETURN)
                else loan.approver if event.action in (Action.APPROVE, Action.REJECT)
                else loan.manager)
    if event.action in (Action.ISSUE, Action.RETURN):
        binding = event.binding
        require(event.evidence_kind == "todo_completion" and binding is not None,
                Code.EVIDENCE)
        require(binding.task == event.source, Code.EVIDENCE)
        require(binding.contact == expected and binding.internal == event.actor,
                Code.WRONG_PERSON)
    else:
        require(event.evidence_kind == "form" and event.source.kind == "form", Code.EVIDENCE)
        require(event.actor == expected, Code.WRONG_PERSON)


def accept_application(loan: Loan, event: Event) -> Loan:
    """Validate trusted, already-read application before it enters the workflow.

    申请行与台账行必须是**同一笔申请**：数量、实物编号之外，归还时间也要对得上
    （``event.due_at`` 是申请行那一格的值）。申请提交后有人改了归还时间就是另一笔
    申请了：不静默按旧时间走，而是可见地跳过（``DUE_AT_MISMATCH``），由人工决定
    重登记还是让申请人重填。申请入口没声明这一格时 ``due_at`` 为 None，比对不成立，
    这是绑定侧的启用检查项（见 docs/deployment.md）。
    """
    require(event.action == Action.APPLY and loan.state == State.AWAITING_APPROVAL, Code.STATE)
    check_event(loan, event)
    require(event.quantity == loan.quantity and event.physical_ids == loan.physical_ids,
            Code.QUANTITY)
    if event.due_at is not None:
        require(event.due_at == loan.due_at, Code.DUE)
    require(loan.due_at > event.occurred_at, Code.INVALID)
    return replace(loan, consumed_events=(event.event_id,), application_evidence=event.evidence_ref)


def move_stock(stock, loan, source, target):
    count = getattr(stock, source)
    require(count >= loan.quantity, Code.CONFLICT)
    changes = {source: count - loan.quantity,
               target: getattr(stock, target) + loan.quantity}
    if loan.tracked:
        origin = getattr(stock, source + "_ids")
        require(set(loan.physical_ids) <= set(origin), Code.CONFLICT)
        changes[source + "_ids"] = tuple(x for x in origin if x not in loan.physical_ids)
        changes[target + "_ids"] = tuple(sorted(getattr(stock, target + "_ids") + loan.physical_ids))
    return replace(stock, **changes)


def plan(loan: Loan, event: Event, stock: Inventory) -> WriteIntent:
    require(bool(loan.application_evidence.strip()), Code.EVIDENCE)
    require(event.loan_ref == loan.ref and stock.ref == loan.item, Code.WRONG_LOAN)
    check_event(loan, event)
    if loan.tracked:
        require(tuple(map(len, (stock.available_ids, stock.reserved_ids, stock.borrowed_ids)))
                == (stock.available, stock.reserved, stock.borrowed), Code.IDENTIFIERS)
    else:
        require(not (stock.available_ids or stock.reserved_ids or stock.borrowed_ids), Code.IDENTIFIERS)
    transitions = {
        Action.APPROVE: (State.AWAITING_APPROVAL, State.RESERVATION_PENDING),
        Action.REJECT: (State.AWAITING_APPROVAL, State.REJECTED),
        Action.RESERVE: (State.RESERVATION_PENDING, State.AWAITING_ISSUE),
        Action.ISSUE: (State.AWAITING_ISSUE, State.BORROWED),
        Action.REQUEST_RETURN: (State.BORROWED, State.AWAITING_RETURN),
        Action.RETURN: (State.AWAITING_RETURN, State.CLOSED),
    }
    next_stock = stock
    if event.action == Action.CANCEL:
        require(loan.state in (State.AWAITING_APPROVAL, State.RESERVATION_PENDING,
                               State.AWAITING_ISSUE), Code.STATE)
        target = State.CANCELLED
        if loan.state == State.AWAITING_ISSUE:
            next_stock = move_stock(stock, loan, "reserved", "available")
    else:
        require(event.action in transitions, Code.STATE)
        source, target = transitions[event.action]
        require(loan.state == source, Code.STATE)
        movements = {Action.RESERVE: ("available", "reserved"),
                     Action.ISSUE: ("reserved", "borrowed"),
                     Action.RETURN: ("borrowed", "available")}
        if event.action in movements:
            next_stock = move_stock(stock, loan, *movements[event.action])
    if event.action == Action.REQUEST_RETURN:
        require(event.return_ref is not None and event.return_ref != loan.ref, Code.WRONG_LOAN)
        require(event.quantity == loan.quantity and event.physical_ids == loan.physical_ids,
                Code.QUANTITY)
    after = replace(loan, state=target,
                    return_ref=event.return_ref if event.action == Action.REQUEST_RETURN else loan.return_ref,
                    consumed_events=loan.consumed_events + (event.event_id,))
    return WriteIntent(operation_id(loan, event), loan, after, stock, next_stock, event)


def verify(intent: WriteIntent, receipt: Receipt) -> Resolution:
    require(receipt.operation_id == intent.operation_id, Code.OP_CONFLICT)
    require(isinstance(receipt.outcome, Outcome), Code.INVALID)
    if receipt.outcome == Outcome.NOT_SENT:
        return Resolution(intent.before, Outcome.NOT_SENT)
    if (receipt.outcome == Outcome.NOT_APPLIED and receipt.readback_evidence.strip()
            and receipt.loan == intent.before and receipt.inventory == intent.stock_before):
        return Resolution(intent.before, Outcome.NOT_APPLIED)
    # revision is an adapter compare token, not part of the desired business values.
    inventory = receipt.inventory
    if (receipt.outcome == Outcome.VERIFIED and receipt.readback_evidence.strip()
            and receipt.loan == intent.after and inventory is not None
            and replace(inventory, revision=intent.stock_after.revision) == intent.stock_after):
        return Resolution(intent.after, Outcome.VERIFIED)
    error = Code.UNKNOWN if receipt.outcome == Outcome.UNKNOWN else Code.READBACK
    return Resolution(intent.before, Outcome.UNKNOWN, error)
