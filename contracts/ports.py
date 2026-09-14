"""Injected interfaces only. No database, OS lock or DingTalk client here."""
from dataclasses import dataclass
import json
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from .flow import Receipt, WriteIntent
from .model import (Action, Event, Identity, IdentityBinding, Inventory, Loan,
                    Resource, Code, Outcome, require, text)


@dataclass(frozen=True)
class RuntimeBinding:
    """Validated deployment snapshot, produced by T04, checked on every write."""
    account: Identity
    ledger: Resource
    config_version: str
    approver: Identity
    manager: Identity
    evidence_ref: str
    ordinary_ledger_denied: bool = False
    restricted_forms_verified: bool = False
    account_read_write_verified: bool = False
    explicitly_confirmed: bool = False


def check_binding(binding: RuntimeBinding, loan: Loan):
    require(binding.account.namespace == "contact"
            and binding.account.tenant_id == loan.ref.tenant_id, Code.IDENTITY)
    require(binding.ledger == loan.item, Code.WRONG_LOAN)
    require(binding.config_version == loan.config_version
            and binding.approver == loan.approver and binding.manager == loan.manager,
            Code.CONFIG)
    require(bool(binding.evidence_ref.strip())
            and all(x is True for x in (binding.ordinary_ledger_denied,
                                       binding.restricted_forms_verified,
                                       binding.account_read_write_verified,
                                       binding.explicitly_confirmed)), Code.EVIDENCE)


class ReadPort(Protocol):
    def read_loan(self, ref: Resource) -> Loan:
        """Exact fresh read; missing/denied/invalid raises ContractError, never empty success."""
        ...

    def read_inventory(self, ref: Resource) -> Inventory:
        """Fresh values plus compare revision (adapter token, not assumed platform CAS)."""
        ...

    def read_event(self, loan: Loan, source: Resource) -> Event:
        """Verify trusted creator, original loan AND phase-specific resource binding.

        Form decisions must be explicit; mutable title/original-ID text is not proof.
        TODO completion needs new activity ID, actor, time, create + readback binding.
        Missing, multiple/conflicting decisions or unverifiable association: fail closed.
        No raw payload or user-supplied verified flag may cross this trust boundary.
        """
        ...


@dataclass(frozen=True)
class StageRequest:
    """Create one restricted human entry; never mark it completed."""
    operation_id: str
    loan: Loan
    action: Action
    actor: Identity

    def __post_init__(self):
        text(self.operation_id)
        require(self.action in (Action.APPROVE, Action.ISSUE, Action.REQUEST_RETURN,
                                Action.RETURN, Action.CANCEL), Code.STATE)
        expected = (self.loan.approver if self.action == Action.APPROVE else
                    self.loan.borrower if self.action == Action.REQUEST_RETURN else self.loan.manager)
        require(self.actor == expected, Code.WRONG_PERSON)


@dataclass(frozen=True)
class StageReceipt:
    operation_id: str
    outcome: Outcome
    source: Resource | None = None
    binding: IdentityBinding | None = None
    creation_evidence: str = ""
    readback_evidence: str = ""


def stage_operation_id(loan: Loan, action: Action) -> str:
    require(isinstance(action, Action))
    parts = (loan.ref.tenant_id, loan.ref.container_id, loan.ref.resource_id,
             "create_stage", action.value)
    return str(uuid5(NAMESPACE_URL, json.dumps(parts, ensure_ascii=True)))


def verify_stage(request: StageRequest, receipt: StageReceipt) -> Outcome:
    require(receipt.operation_id == request.operation_id, Code.OP_CONFLICT)
    require(isinstance(receipt.outcome, Outcome))
    if receipt.outcome == Outcome.NOT_SENT:
        return Outcome.NOT_SENT
    source = receipt.source
    if (receipt.outcome != Outcome.VERIFIED or source is None
            or not receipt.creation_evidence.strip() or not receipt.readback_evidence.strip()
            or source.tenant_id != request.loan.ref.tenant_id):
        return Outcome.UNKNOWN
    if request.action in (Action.ISSUE, Action.RETURN):
        binding = receipt.binding
        if (source.kind != "todo" or binding is None or binding.task != source
                or binding.contact != request.actor):
            return Outcome.UNKNOWN
    elif source.kind != "form":
        return Outcome.UNKNOWN
    return Outcome.VERIFIED


class StagePort(Protocol):
    def create_stage(self, request: StageRequest, binding: RuntimeBinding, lease: str) -> StageReceipt:
        """Side effect: restricted explicit decision form or separate confirmation TODO.

        APPROVE form supports both approve/reject, never derives a decision from done.
        ISSUE and RETURN require separate task IDs. Persist request and phase/source
        binding before exposing link. VERIFIED requires creation + independent exact
        resource/config readback; TODO also needs IdentityBinding. Acknowledgment only
        is UNKNOWN. Same ID/different request conflicts. Unknown must query, not create.
        No universal approval capability is assumed; unverifiable links fail closed.
        """
        ...

    def query_stage(self, request: StageRequest) -> StageReceipt:
        """Read-only reconciliation of original creation, recipient and original loan/phase."""
        ...


class WritePort(Protocol):
    def submit(self, intent: WriteIntent, binding: RuntimeBinding, lease: str) -> Receipt:
        """Side effects. Validate current authority/lease/binding and fresh preconditions.

        Persist intent before send; apply at most once per operation_id and payload.
        Receipt is UNKNOWN until independent exact loan AND stock readback matches.
        Partial writes are UNKNOWN, not NOT_APPLIED; never replay the whole intent.
        Stale revision/counts/IDs: no writes, RESERVATION_CONFLICT.
        This is single-writer compare-before-write, NOT a platform transaction.
        """
        ...

    def query(self, intent: WriteIntent) -> Receipt:
        """Read-only reconciliation by operation ID, all targets and expected values.

        Not found, delayed indexes or equal stock alone cannot prove not applied.
        NOT_APPLIED requires conclusive evidence for every target/no send in flight.
        Unresolved/partial: keep UNKNOWN and stop mutations on this item/loan.
        """
        ...


class OperationStore(Protocol):
    def prepare(self, intent: WriteIntent | StageRequest) -> None:
        """Durably save exact intent before sending. Same ID/different payload rejects.

        Must reject another unresolved operation sharing loan/item. Store no second
        authoritative inventory: snapshots only for recovery against DingTalk.
        """
        ...

    def load(self, operation_id: str) -> tuple[WriteIntent | StageRequest, Receipt | StageReceipt | None]:
        """Restart reads original intent, never recomputes it from changed config."""
        ...

    def save_receipt(self, receipt: Receipt | StageReceipt) -> None:
        """Durable result; a send-start marker is UNKNOWN before network invocation."""
        ...


class SingleInstance(Protocol):
    def acquire(self, ledger: Resource, account: Identity) -> str:
        """Machine-wide exclusive writer, covering ALL users/processes for this ledger.

        Second instance raises SECOND_INSTANCE_BLOCKED even with a different runtime
        directory or account. Lease held for entire running lifetime. No TTL takeover.
        Startup with unresolved journal: reconcile first. Cross-machine unsupported.
        """
        ...

    def assert_held(self, lease: str) -> None:
        """Every write must fail closed on missing/lost lease."""
        ...

    def release(self, lease: str) -> None:
        """Only owner releases after writes stop and recovery records are flushed."""
        ...
