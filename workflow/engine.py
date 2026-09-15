"""Lending-loop orchestration over the frozen contracts.

The engine wires injected ports (ReadPort/WritePort/StagePort from T03,
SingleInstance/OperationStore persistence from T04): it never talks to a
real platform, never invents fields or states, and publishes a new state
only from a readback-verified resolution. plan() stays pure; every write
goes through prepare -> submit -> verify and survives restart via the
operation store.
"""
from dataclasses import dataclass

from contracts.flow import Resolution, accept_application, plan, verify
from contracts.model import Action, Event, Outcome
from contracts.ports import (StageRequest, check_binding, stage_operation_id,
                             verify_stage)


@dataclass(frozen=True)
class Execution:
    """Resolution plus the operation id needed to reconcile an unknown."""

    operation_id: str
    resolution: Resolution

    @property
    def outcome(self):
        return self.resolution.outcome

    @property
    def loan(self):
        return self.resolution.loan

    @property
    def error(self):
        return self.resolution.error

_STAGE_ACTORS = {Action.APPROVE: lambda loan: loan.approver,
                 Action.ISSUE: lambda loan: loan.manager,
                 Action.REQUEST_RETURN: lambda loan: loan.borrower,
                 Action.RETURN: lambda loan: loan.manager,
                 Action.CANCEL: lambda loan: loan.manager}


class LendingEngine:
    """Single-writer lending loop; one instance per running process."""

    def __init__(self, reader, writer, stages, store, single, binding):
        self.reader = reader
        self.writer = writer
        self.stages = stages
        self.store = store
        self.single = single
        self.binding = binding
        self.lease = None

    def start(self, scope, account):
        self.lease = self.single.acquire(scope, account)

    def stop(self):
        self.single.release(self.lease)
        self.lease = None

    def admit_application(self, loan_ref, source):
        """Validate a trusted application read through the ReadPort."""
        loan = self.reader.read_loan(loan_ref)
        event = self.reader.read_event(loan, source)
        return accept_application(loan, event)

    def execute(self, loan_ref, source):
        """Human decision/confirmation read from a trusted source."""
        loan = self.reader.read_loan(loan_ref)
        event = self.reader.read_event(loan, source)
        return self._run(plan(loan, event, self.reader.read_inventory(loan.item)))

    def reserve(self, loan_ref, approval_event):
        """System reservation right after an approved application."""
        loan = self.reader.read_loan(loan_ref)
        stock = self.reader.read_inventory(loan.item)
        event = Event(Action.RESERVE, 'system:' + approval_event.event_id,
                      loan.ref, approval_event.source, None,
                      approval_event.occurred_at, loan.config_version,
                      'system', True, evidence_ref=approval_event.evidence_ref)
        return self._run(plan(loan, event, stock))

    def ensure_stage(self, loan, action):
        """Create the restricted human entry once per loan/phase."""
        request = StageRequest(stage_operation_id(loan, action), loan, action,
                               _STAGE_ACTORS[action](loan))
        _, receipt = self._load(request.operation_id)
        if receipt is not None:
            outcome = verify_stage(request, receipt)
            if outcome == Outcome.VERIFIED:
                return receipt
            if outcome == Outcome.UNKNOWN:
                # Unknown must query, never create a second entry.
                receipt = self.stages.query_stage(request)
                self.store.save_receipt(receipt)
                verify_stage(request, receipt)
                return receipt
        self.single.assert_held(self.lease)
        self.store.prepare(request)
        receipt = self.stages.create_stage(request, self.binding, self.lease)
        verify_stage(request, receipt)
        self.store.save_receipt(receipt)
        return receipt

    def resolve(self, operation_id):
        """Reconcile an unresolved operation after restart or unknown."""
        intent, receipt = self.store.load(operation_id)
        if isinstance(intent, StageRequest):
            if receipt is not None and verify_stage(intent, receipt) != Outcome.UNKNOWN:
                return receipt
            receipt = self.stages.query_stage(intent)
            self.store.save_receipt(receipt)
            verify_stage(intent, receipt)
            return receipt
        if receipt is not None and receipt.outcome != Outcome.UNKNOWN:
            return verify(intent, receipt)
        receipt = self.writer.query(intent)
        resolution = verify(intent, receipt)
        self.store.save_receipt(receipt)
        return resolution

    def _run(self, intent):
        self.single.assert_held(self.lease)
        check_binding(self.binding, intent.before)
        self.store.prepare(intent)
        receipt = self.writer.submit(intent, self.binding, self.lease)
        resolution = verify(intent, receipt)
        self.store.save_receipt(receipt)
        return Execution(intent.operation_id, resolution)

    def _load(self, operation_id):
        try:
            return self.store.load(operation_id)
        except KeyError:
            return None, None
