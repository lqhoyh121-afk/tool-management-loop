"""Non-interactive lending driver: recover journal, then consume trusted sources.

Does not modify workflow/ or contracts/. The engine and ports are injected.
Live DingTalk wiring belongs to T07 (dws_cmd must be supplied; this module
never guesses an install path). Ledger and stage-entry field maps both come
from binding.json; neither falls back to synthetic IDs.
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
    """One pass, with stage reconciliation kept in its own account.

    ``recovered`` is only the unresolved-journal recovery; the entry points the
    reconcile created are writes, so they are reported by ``stage_created``
    (operation ids) and never mixed into the recovery count. ``stage_checked``
    holds the known loan refs the reconcile examined. Reconcile skips travel in
    ``skipped`` like every other skip, tagged ``kind='stage'``.
    """

    recovered: tuple
    processed: tuple
    skipped: tuple
    blocked: tuple
    stage_checked: tuple = ()
    stage_created: tuple = ()


def _drive_outcome(item, code):
    return DriveOutcome(
        kind=item.kind,
        loan_id=item.loan_ref.resource_id,
        source_kind=item.source.kind,
        source_id=item.source.resource_id,
        code=code,
    )


def _stage_outcome(loan_ref, operation_id, code):
    """One reconcile result in the same shape every other skip uses.

    The source column carries the stage operation id when the pass got far
    enough to derive one, otherwise the loan id.
    """
    return DriveOutcome('stage', loan_ref.resource_id, 'stage',
                        operation_id or loan_ref.resource_id, code.value)


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


def _stage_skips(outcomes):
    """Reconcile skips share the skip tuple with queue skips, tagged by kind."""
    return tuple(o for o in outcomes if o.kind == 'stage')


def format_drive_lines(report):
    waiting = _waiting_on_human(report.skipped)
    lines = [
        '驱动完成。回查 {recovered}，处理 {processed}，跳过 {skipped}，'
        '挂起 {blocked}。未盲重发。'.format(
            recovered=len(report.recovered),
            processed=len(report.processed),
            skipped=format_outcome_summary(report.skipped),
            blocked=format_outcome_summary(report.blocked),
        ),
        '阶段对账：检查 {checked}，新建 {created}，跳过 {skipped}'.format(
            checked=len(report.stage_checked),
            created=len(report.stage_created),
            skipped=len(_stage_skips(report.skipped)),
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
        stage_checked, stages_created, stage_skips = self._reconcile_stages()
        skipped.extend(stage_skips)
        blocked_loans = set(self._unresolved_loans())
        for item in self._work():
            if item.loan_ref in blocked_loans:
                blocked.append(_drive_outcome(item, Code.UNKNOWN.value))
                continue
            try:
                self._handle(item)
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
        return DriveReport(tuple(recovered), tuple(processed), tuple(skipped), tuple(blocked),
                           tuple(stage_checked), tuple(stages_created))

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
        execution = self.engine.execute(loan_ref, item.source)
        if execution.outcome == Outcome.UNKNOWN:
            raise ContractError(Code.UNKNOWN)
        if execution.outcome != Outcome.VERIFIED:
            return
        current = execution.loan
        if current.state == State.RESERVATION_PENDING:
            event = self.engine.reader.read_event(current, item.source)
            reserved = self.engine.reserve(current.ref, event)
            if reserved.outcome == Outcome.UNKNOWN:
                raise ContractError(Code.UNKNOWN)
            if reserved.outcome != Outcome.VERIFIED:
                return
            current = reserved.loan
        self._ensure_current_stage(current)

    def _known_loans(self):
        """Loan refs the queue or the journal already mentions, first-seen order.

        Both journal entry kinds count: a verified stage receipt names the loan
        whose entry was already built, and a write intent names the loan a
        ledger write addressed, whatever its receipt says. Reading only verified
        stage receipts missed every loan whose sole trace is a write intent —
        the local index dropping that one record is enough — and such a loan
        never got its entry point rebuilt.
        """
        refs = []
        seen = set()
        for item in self._work():
            if item.loan_ref not in seen:
                seen.add(item.loan_ref)
                refs.append(item.loan_ref)
        for ref in self._journal_loan_refs():
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
        return tuple(refs)

    def _journal_loan_refs(self):
        """Loan refs carried by journal write intents, in operation-id order."""
        refs = []
        for operation_id in self.store.ids():
            try:
                intent, _ = self.store.load(operation_id)
            except KeyError:
                continue
            if isinstance(intent, StageRequest):
                continue
            refs.append(intent.before.ref)
        return tuple(refs)

    def _reconcile_stages(self):
        """Create the current stage for known loans that no item drives here.

        A loan can reach a stage-bearing state without this pass witnessing the
        transition: its queue entries were already consumed, or it was lent out
        outside the drive. Without the entry the human has nothing to act on and
        the loan stalls, so reconcile by ``stage_operation_id`` — an existing
        receipt (verified or unknown) is left alone, never recreated.

        Accounting is split into three answers, because an operator must be able
        to tell a write from a recovery: ``checked`` are the known loans this
        pass looked at, ``created`` are the entry points it actually created
        now (never counted as a recovery), and ``skipped`` is one reported
        outcome per loan this pass found and could not act on — unreadable loan,
        a state with no next human stage, or a failing binding check. An entry
        that already exists is the steady state and is not a skip.

        Failures stay per-loan: a loan this pass cannot act on becomes one
        reported skip, exactly like a bad queue item, and never aborts the pass
        (an abort here would also stop the queue work that follows).
        """
        checked = []
        created = []
        skipped = []
        for ref in self._known_loans():
            checked.append(ref)
            operation_id = None
            try:
                loan = self.engine.reader.read_loan(ref)
                action = _NEXT_STAGE.get(loan.state)
                if action is None:
                    skipped.append(_stage_outcome(ref, None, Code.STATE))
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
                skipped.append(_stage_outcome(ref, operation_id, exc.code))
                continue
            created.append(operation_id)
        return tuple(checked), tuple(created), tuple(skipped)

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
