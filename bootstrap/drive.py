"""Non-interactive lending driver: recover journal, then consume trusted sources.

Does not modify workflow/ or contracts/. The engine and ports are injected.
Live DingTalk wiring belongs to T07 (dws_cmd must be supplied; this module
never guesses an install path). Ledger and stage-entry field maps both come
from binding.json; neither falls back to synthetic IDs.

A crash between the approval write and the system reservation leaves the loan
in `reservation_pending` with the approval event already consumed, so every
later pass used to report nothing but DUPLICATE_EVENT. See
`_finish_pending_reservation`.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from contracts.flow import Outcome
from contracts.model import Action, Code, ContractError, State, require
from contracts.ports import StageRequest, check_binding, stage_operation_id
from workflow.engine import LendingEngine

from .binding import (binding_from_document, field_maps_from_document,
                      read_binding_document, require_complete)
from .gate import assert_business_allowed
from .instance import MachineLock
from .journal import FileJournal
from .snapshot import decode_resource, encode_resource

_NEXT_STAGE = {
    State.AWAITING_APPROVAL: Action.APPROVE,
    State.AWAITING_ISSUE: Action.ISSUE,
    State.BORROWED: Action.REQUEST_RETURN,
    State.AWAITING_RETURN: Action.RETURN,
}


class _Pending(ContractError):
    """Half-finished transition the driver must neither retry nor hide.

    Raised only where the ledger is between two external writes and the driver
    cannot prove enough to finish the step. run() reports it in `blocked`, so
    the loan stays visible to a human instead of looking like a plain
    duplicate event on every pass.
    """


@dataclass(frozen=True)
class WorkItem:
    kind: str
    loan_ref: object
    source: object

    def __post_init__(self):
        if self.kind not in ('apply', 'event'):
            raise ValueError(self.kind)


@dataclass(frozen=True)
class DriveOutcome:
    kind: str
    loan_id: str
    source_kind: str
    source_id: str
    code: str


@dataclass(frozen=True)
class DriveReport:
    recovered: tuple
    processed: tuple
    skipped: tuple
    blocked: tuple


def _drive_outcome(item, code):
    return DriveOutcome(
        kind=item.kind,
        loan_id=item.loan_ref.resource_id,
        source_kind=item.source.kind,
        source_id=item.source.resource_id,
        code=code,
    )


def _code_counts(outcomes):
    counts = {}
    for outcome in outcomes:
        counts[outcome.code] = counts.get(outcome.code, 0) + 1
    return counts


def format_outcome_summary(outcomes):
    if not outcomes:
        return '0'
    parts = [f'{code}×{count}' for code, count in sorted(_code_counts(outcomes).items())]
    return f'{len(outcomes)}（{"，".join(parts)}）'


def _waiting_on_human(outcomes):
    """Skips that really mean "the human has not acted yet".

    An open stage todo reads as ``STATE`` from the todo channel (no completion
    event yet) — the reader marks that case separately on purpose, because a
    *completed* todo whose evidence is unreadable still comes back as
    ``EVIDENCE``. Only the former is "等人工"; calling a broken completion
    "证据不足" would be wrong the other way round.
    """
    return tuple(o for o in outcomes
                 if o.code == Code.STATE.value and o.source_kind == 'todo')


def format_drive_lines(report):
    waiting = _waiting_on_human(report.skipped)
    lines = [
        '驱动完成。回查 {recovered}（含阶段对账新建），处理 {processed}，跳过 {skipped}，'
        '挂起 {blocked}。未盲重发。'.format(
            recovered=len(report.recovered),
            processed=len(report.processed),
            skipped=format_outcome_summary(report.skipped),
            blocked=format_outcome_summary(report.blocked),
        ),
    ]
    if waiting:
        ids = '，'.join(o.source_id for o in waiting)
        lines.append(f'  其中待人工 {len(waiting)} 条（阶段待办尚未完成，不是证据不足）：{ids}')
    for label, outcomes in (('跳过', report.skipped), ('挂起', report.blocked)):
        for outcome in outcomes:
            lines.append(
                f'  {label} {outcome.kind} loan={outcome.loan_id} '
                f'{outcome.source_kind}={outcome.source_id} {outcome.code}'
            )
    return lines


class FileSources:
    """Reference queue of loan/source pairs. Not an inventory ledger."""

    def __init__(self, path):
        self.path = Path(path)

    def pending(self):
        if not self.path.exists():
            return ()
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, list):
            raise ContractError(Code.INVALID)
        items = []
        for raw in data:
            items.append(WorkItem(raw['kind'], decode_resource(raw['loan']),
                                  decode_resource(raw['source'])))
        return tuple(items)


class StaticSources:
    def __init__(self, items):
        self._items = tuple(items)

    def pending(self):
        return self._items


class DriveLoop:
    """One non-interactive pass. Restart always reconciles unresolved ops first."""

    def __init__(self, engine, sources, store, locks):
        self.engine = engine
        self.sources = sources
        self.store = store
        self.locks = locks

    def run(self):
        assert_business_allowed(self.engine.binding, self.engine.lease, self.locks)
        processed = []
        skipped = []
        blocked = []
        recovered = self._recover()
        stages_created, stage_skips = self._reconcile_stages()
        skipped.extend(stage_skips)
        blocked_loans = set(self._unresolved_loans())
        for item in self._work():
            if item.loan_ref in blocked_loans:
                blocked.append(_drive_outcome(item, Code.UNKNOWN.value))
                continue
            try:
                self._handle(item)
            except _Pending as exc:
                blocked_loans.add(item.loan_ref)
                blocked.append(_drive_outcome(item, exc.code.value))
                continue
            except ContractError as exc:
                if exc.code == Code.INSTANCE:
                    raise
                if exc.code == Code.UNKNOWN:
                    blocked_loans.add(item.loan_ref)
                    blocked.append(_drive_outcome(item, Code.UNKNOWN.value))
                    continue
                skipped.append(_drive_outcome(item, exc.code.value))
                continue
            processed.append(item)
        recovered.extend(stages_created)
        return DriveReport(tuple(recovered), tuple(processed), tuple(skipped), tuple(blocked))

    def _recover(self):
        recovered = []
        for operation_id in self.store.unresolved_ids():
            assert_business_allowed(self.engine.binding, self.engine.lease, self.locks)
            self.engine.resolve(operation_id)
            recovered.append(operation_id)
        return recovered

    def _unresolved_loans(self):
        refs = []
        for operation_id in self.store.unresolved_ids():
            intent, _ = self.store.load(operation_id)
            if isinstance(intent, StageRequest):
                refs.append(intent.loan.ref)
            else:
                refs.append(intent.before.ref)
        return tuple(refs)

    def _work(self):
        seen = set()
        items = []
        for item in tuple(self.sources.pending()) + self._stage_items():
            key = (item.kind, item.loan_ref, item.source)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        return tuple(items)

    def _stage_items(self):
        items = []
        for operation_id in self.store.ids():
            intent, receipt = self.store.load(operation_id)
            if not isinstance(intent, StageRequest):
                continue
            if receipt is None or receipt.outcome != Outcome.VERIFIED or receipt.source is None:
                continue
            items.append(WorkItem('event', intent.loan.ref, receipt.source))
        return tuple(items)

    def _resolve_loan_ref(self, item):
        resolver = getattr(self.engine.reader, 'resolve_return_form_loan', None)
        if resolver is None:
            return item.loan_ref
        matched = resolver(item.source, item.loan_ref)
        if matched is None:
            return item.loan_ref
        return matched

    def _handle(self, item):
        loan_ref = self._resolve_loan_ref(item)
        loan = self.engine.reader.read_loan(loan_ref)
        check_binding(self.engine.binding, loan)
        self.locks.assert_held(self.engine.lease)
        if item.kind == 'apply':
            self.engine.admit_application(loan_ref, item.source)
            current = self.engine.reader.read_loan(loan_ref)
            self._ensure_current_stage(current)
            return
        try:
            execution = self.engine.execute(loan_ref, item.source)
        except ContractError as exc:
            if exc.code != Code.DUPLICATE or loan.state != State.RESERVATION_PENDING:
                raise
            self._finish_pending_reservation(item, loan)
            return
        if execution.outcome == Outcome.UNKNOWN:
            raise ContractError(Code.UNKNOWN)
        if execution.outcome != Outcome.VERIFIED:
            return
        current = execution.loan
        if current.state == State.RESERVATION_PENDING:
            event = self.engine.reader.read_event(current, item.source)
            self._reserve(current, event)
            return
        self._ensure_current_stage(current)

    def _reserve(self, loan, event):
        """Run the system reservation, then expose the next human stage."""
        reserved = self.engine.reserve(loan.ref, event)
        if reserved.outcome == Outcome.UNKNOWN:
            raise ContractError(Code.UNKNOWN)
        if reserved.outcome != Outcome.VERIFIED:
            return
        self._ensure_current_stage(reserved.loan)

    def _finish_pending_reservation(self, item, loan):
        """审批已写、预留未写：finish the reservation instead of only reporting
        DUPLICATE_EVENT on every pass.

        The approval event is already in the loan's `consumed_events`, so the
        engine refuses to re-plan it and the loan would sit in
        `reservation_pending` with no entry point for a human. The approval write
        itself is durable in the journal, so the reservation is re-derived from
        that recorded event (never from a mutable decision row):

        * the reserve event id derives from the approval event id
          (`LendingEngine.reserve`), so the retry addresses the same
          operation_id as the original attempt; the store rejects a
          same-id/different-payload intent and unresolved writes are queried
          instead of replayed, so nothing is submitted twice;
        * plan() refuses RESERVE unless the fresh loan read still says
          `reservation_pending` and the reserve event was never consumed, so an
          already-applied reservation cannot move stock again;
        * the stock move is compare-before-write on a fresh inventory read with
          quantity and physical-id preconditions, not a blind increment.

        Unprovable evidence is reported as a blocked item, never retried.
        """
        event = self._recorded_approval(loan, item.source)
        if event is None:
            raise _Pending(Code.STATE)
        self._reserve(loan, event)

    def _recorded_approval(self, loan, source):
        """The applied approval event for this loan, or None when unprovable.

        Only a readback-verified approval write whose event the ledger already
        consumed qualifies; zero or several candidates fail closed.
        """
        found = []
        for operation_id in self.store.ids():
            intent, receipt = self.store.load(operation_id)
            if isinstance(intent, StageRequest) or intent.event.action != Action.APPROVE:
                continue
            if intent.before.ref != loan.ref or intent.event.source != source:
                continue
            if receipt is None or receipt.outcome != Outcome.VERIFIED:
                continue
            if intent.event.event_id not in loan.consumed_events:
                continue
            found.append(intent.event)
        return found[0] if len(found) == 1 else None

    def _known_loans(self):
        """Loan refs the queue or the journal already mentions, first-seen order."""
        refs = []
        seen = set()
        for item in self._work():
            if item.loan_ref not in seen:
                seen.add(item.loan_ref)
                refs.append(item.loan_ref)
        return tuple(refs)

    def _reconcile_stages(self):
        """Create the current stage for known loans that no item drives here.

        A loan can reach a stage-bearing state without this pass witnessing the
        transition: its queue entries were already consumed, or it was lent out
        outside the drive. Without the entry the human has nothing to act on and
        the loan stalls, so reconcile by ``stage_operation_id`` — an existing
        receipt (verified or unknown) is left alone, never recreated.

        Failures stay per-loan: a loan this pass cannot act on becomes one
        reported skip, exactly like a bad queue item, and never aborts the pass
        (an abort here would also stop the queue work that follows).
        """
        created = []
        skipped = []
        for ref in self._known_loans():
            operation_id = None
            try:
                loan = self.engine.reader.read_loan(ref)
                action = _NEXT_STAGE.get(loan.state)
                if action is None:
                    continue
                operation_id = stage_operation_id(loan, action)
                try:
                    _, receipt = self.store.load(operation_id)
                except KeyError:
                    receipt = None
                if receipt is not None:
                    continue
                self.locks.assert_held(self.engine.lease)
                check_binding(self.engine.binding, loan)
                self.engine.ensure_stage(loan, action)
            except ContractError as exc:
                if exc.code == Code.INSTANCE:
                    raise
                skipped.append(DriveOutcome('stage', ref.resource_id, 'stage',
                                            operation_id or ref.resource_id,
                                            exc.code.value))
                continue
            created.append(operation_id)
        return created, tuple(skipped)

    def _ensure_current_stage(self, loan):
        action = _NEXT_STAGE.get(loan.state)
        if action is None:
            return
        self.locks.assert_held(self.engine.lease)
        check_binding(self.engine.binding, loan)
        self.engine.ensure_stage(loan, action)


def start_engine(reader, writer, stages, store, locks, binding):
    engine = LendingEngine(reader, writer, stages, store, locks, binding)
    engine.start(binding.ledger, binding.account)
    return engine


def live_adapter(runtime, journal, locks, document, fields=None, entry_fields=None,
                 apply_fields=None, application_container=None,
                 return_form_fields=None, loan_container=None):
    """Build DingTalkAdapter only from explicit binding fields. Never guess dws."""
    from integrations.dingtalk.adapter import DingTalkAdapter
    from integrations.dingtalk.dws_transport import DwsTransport

    cmd = document.get('dws_cmd')
    require(isinstance(cmd, list) and all(isinstance(part, str) and part for part in cmd),
            Code.CONFIG)
    form_container = document.get('form_container')
    todo_container = document.get('todo_container')
    require(isinstance(form_container, str) and form_container.strip(), Code.CONFIG)
    require(isinstance(todo_container, str) and todo_container.strip(), Code.CONFIG)
    if (fields is None or entry_fields is None or apply_fields is None
            or return_form_fields is None):
        fields, entry_fields, apply_fields, return_form_fields = field_maps_from_document(
            document)
    if application_container is None:
        entry = document.get('application_entry')
        require(isinstance(entry, dict), Code.CONFIG)
        application_container = entry.get('container_id')
        require(isinstance(application_container, str) and application_container.strip(),
                Code.CONFIG)
    if loan_container is None:
        loan_container = document.get('loan_container')
        require(isinstance(loan_container, str) and loan_container.strip(), Code.CONFIG)
    transport = DwsTransport(
        cmd, fields, form_container=form_container, todo_container=todo_container,
        work_dir=runtime, entry_fields=entry_fields,
    )
    return DingTalkAdapter(
        transport, journal, locks, fields, entry_fields,
        apply_fields=apply_fields, application_container=application_container,
        return_form_fields=return_form_fields, entry_container=form_container,
        loan_container=loan_container,
    )


def run_bound_drive(runtime, lock_root, reader=None, writer=None, stages=None,
                    sources=None, locks=None, store=None):
    runtime = Path(runtime)
    document = read_binding_document(runtime)
    if document is None:
        binding, _entry, fields, entry_fields, apply_fields, return_form_fields = (
            None, None, None, None, None, None)
    else:
        binding, _entry, fields, entry_fields, apply_fields, return_form_fields = (
            binding_from_document(document))
    require_complete(binding)
    locks = locks or MachineLock(lock_root)
    store = store or FileJournal(runtime / 'operations')
    if reader is None or writer is None or stages is None:
        adapter = live_adapter(runtime, store, locks, document, fields, entry_fields)
        reader = writer = stages = adapter
    if sources is None:
        sources = FileSources(runtime / 'sources.json')
    engine = start_engine(reader, writer, stages, store, locks, binding)
    try:
        return DriveLoop(engine, sources, store, locks).run()
    finally:
        engine.stop()


def dump_sources(path, items):
    payload = [{'kind': item.kind, 'loan': encode_resource(item.loan_ref),
                'source': encode_resource(item.source)} for item in items]
    path = Path(path)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + '\n',
                    encoding='utf-8')
