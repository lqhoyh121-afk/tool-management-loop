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
from dataclasses import dataclass, replace
from pathlib import Path

from contracts.flow import Outcome
from contracts.model import Action, Code, ContractError, State, require
from contracts.ports import StageRequest, check_binding, stage_operation_id
from workflow.engine import LendingEngine

from .binding import (binding_from_document, field_maps_from_document,
                      intake_since_from_document, read_binding_document, require_complete)
from .gate import assert_business_allowed
from .inbox import (REGISTER_NAME, ApplicationIntake, IntakeReport, IntakeSkip,
                    format_intake_lines)
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
    """One pass, with stage reconciliation kept in its own account.

    ``recovered`` is only the unresolved-journal recovery; the entry points the
    reconcile created are writes, so they are reported by ``stage_created``
    (operation ids) and never mixed into the recovery count. ``stage_checked``
    holds the known loan refs the reconcile examined. Reconcile skips travel in
    ``skipped`` like every other skip, tagged ``kind='stage'``.

    ``healed`` is not a fourth bucket: every healed loan also appears in
    ``processed``, because the pass really did write its reservation. The entry
    carries the read failure the queue row reported (``code``), which is why the
    heal ran, and never decided whether it ran.

    ``intake`` is the application discovery account for this pass (issue #78) or
    None when discovery is not wired. Discovered applications enter the queue
    like any other work item; the account only says what was scanned,
    registered and skipped. A discovery that blew up is reported inside that
    account (``scan_code``), never as a failed pass: the recovery and the stage
    reconcile below still run on the work the queue already holds.
    """

    recovered: tuple
    processed: tuple
    skipped: tuple
    blocked: tuple
    stage_checked: tuple = ()
    stage_created: tuple = ()
    healed: tuple = ()
    intake: object = None


def _failed_intake(code, note='发现扫描抛异常，本轮未登记'):
    """发现阶段没跑完的账目：本轮不登记任何申请，但这一轮照常继续。

    ``scan_code`` 非空即「查表没结论」，报告里与「扫到 0 行、表真的空」区分开。
    """
    return IntakeReport(scan_code=code, scan_note=note,
                        skipped=(IntakeSkip('-', code),))


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
    if report.healed:
        lines.append(
            '  自愈补齐 {summary}，按已落库审批补写预留'.format(
                summary=format_outcome_summary(report.healed))
        )
    if report.intake is not None:
        lines.extend(format_intake_lines(report.intake))
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

    def append(self, registrations):
        """Register discovered applications in the queue; existing pairs are kept.

        This is the durable half of 「登记 apply 引用」: the queue is what the next
        pass reconciles stages from, so a pair that is already there is never
        written twice (idempotent under restarts and re-scans).
        """
        current = list(self.pending())
        seen = {(item.kind, item.loan_ref, item.source) for item in current}
        added = []
        for registration in registrations:
            item = WorkItem(registration.kind, registration.loan_ref, registration.source)
            key = (item.kind, item.loan_ref, item.source)
            if key in seen:
                continue
            seen.add(key)
            added.append(item)
        if not added:
            return ()
        payload = [{'kind': item.kind, 'loan': encode_resource(item.loan_ref),
                    'source': encode_resource(item.source)} for item in current + added]
        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + '\n'
        tmp = self.path.with_suffix('.tmp')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload_text, encoding='utf-8')
        tmp.replace(self.path)
        return tuple(added)


class StaticSources:
    def __init__(self, items):
        self._items = tuple(items)

    def pending(self):
        return self._items


class DriveLoop:
    """One non-interactive pass. Restart always reconciles unresolved ops first."""

    def __init__(self, engine, sources, store, locks, intake=None):
        self.engine = engine
        self.sources = sources
        self.store = store
        self.locks = locks
        self.intake = intake

    def run(self):
        """One pass, with ``blocked_loans`` limited to genuinely unresolved writes.

        The set carries the journal's unresolved operation ids plus the loan of
        an item that just failed with ``UNKNOWN`` — the only case where
        ``WRITE_UNKNOWN_QUERY_FIRST`` about a sibling entry is true. A loan
        blocked for any other reason (half-finished transition, unreadable
        evidence, a stock snapshot that no longer matches the approval) must not
        spread: the sibling entries of that loan are still evaluated against
        their own fresh read, otherwise one bad row starves the entry that can
        finish the transition and reports every sibling under a code that lies.
        """
        assert_business_allowed(self.engine.binding, self.engine.lease, self.locks)
        processed = []
        skipped = []
        blocked = []
        # 申请发现先于队列消费：真人新提交的行必须在**同一轮**里变成审批入口，
        # 而不是等操作员登记。发现本身也只写本机队列与台账行，闸门照旧。
        reported = self._run_intake()
        discovered = tuple(WorkItem(finding.kind, finding.loan_ref, finding.source)
                           for finding in getattr(reported, 'findings', ()))
        healed = []
        recovered = self._recover()
        stage_checked, stages_created, stage_skips = self._reconcile_stages(discovered)
        skipped.extend(stage_skips)
        blocked_loans = set(self._unresolved_loans())
        for item in self._work(discovered):
            if item.loan_ref in blocked_loans:
                blocked.append(_drive_outcome(item, Code.UNKNOWN.value))
                continue
            try:
                explanation = self._handle(item)
            except _Pending as exc:
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
            if explanation is not None:
                healed.append(_drive_outcome(item, explanation.value))
        return DriveReport(tuple(recovered), tuple(processed), tuple(skipped), tuple(blocked),
                           tuple(stage_checked), tuple(stages_created),
                           tuple(healed), reported)

    def _run_intake(self):
        """这一轮的申请发现账目；没接发现就是 None。

        发现是**便利**，不是这一轮本身：只有实例闸门（``SECOND_INSTANCE_BLOCKED``）
        才允许带着整轮一起停，其余任何异常都记进发现账目（``scan_code``）并按「本轮
        未登记」继续 —— 一趟坏掉的发现不该把日志恢复与阶段对账一起带走。
        """
        if self.intake is None:
            return None
        try:
            return self.intake.run()
        except ContractError as exc:
            if exc.code == Code.INSTANCE:
                raise
            return _failed_intake(exc.code.value, note='发现扫描未完成，本轮未登记')
        except Exception:
            return _failed_intake(Code.UNKNOWN.value)

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

    def _work(self, extra=()):
        seen = set()
        items = []
        for item in tuple(self.sources.pending()) + tuple(extra) + self._stage_items():
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
        """One queue item; returns the read failure a heal had to work around.

        An item normally drives the transition its own row carries. The
        exception is a loan whose row still reads ``reservation_pending``: there
        this row's evidence is not the judge — see
        `_finish_pending_reservation` — and the original failure travels back to
        the report as an explanation, never as the decision.
        """
        loan_ref = self._resolve_loan_ref(item)
        loan = self.engine.reader.read_loan(loan_ref)
        check_binding(self.engine.binding, loan)
        self.locks.assert_held(self.engine.lease)
        if item.kind == 'apply':
            self.engine.admit_application(loan_ref, item.source)
            current = self.engine.reader.read_loan(loan_ref)
            self._ensure_current_stage(current)
            return None
        try:
            execution = self.engine.execute(loan_ref, item.source)
        except ContractError as exc:
            if exc.code in (Code.INSTANCE, Code.UNKNOWN):
                raise
            if loan.state != State.RESERVATION_PENDING:
                raise
            return self._finish_pending_reservation(loan, exc.code)
        if execution.outcome == Outcome.UNKNOWN:
            raise ContractError(Code.UNKNOWN)
        if execution.outcome != Outcome.VERIFIED:
            # 平台明确回报这次写没有落地（NOT_SENT/NOT_APPLIED）：跳过它，
            # 不能静默计成「已处理」——单还停在原地，人要看得见。
            raise ContractError(Code.READBACK)
        current = execution.loan
        if current.state == State.RESERVATION_PENDING:
            event = self.engine.reader.read_event(current, item.source)
            return self._reserve(current, event)
        self._ensure_current_stage(current)
        return None

    def _reserve(self, loan, event, original=None):
        """Run the system reservation, then expose the next human stage.

        Returns the read failure the caller was working around (``original``),
        so a heal can be reported as a heal. A reservation the platform reports
        as never sent or never applied is a skip, not a processed item.
        """
        reserved = self.engine.reserve(loan.ref, event)
        if reserved.outcome == Outcome.UNKNOWN:
            raise ContractError(Code.UNKNOWN)
        if reserved.outcome in (Outcome.NOT_SENT, Outcome.NOT_APPLIED):
            # 预留没落地：跳过并留给下一轮，别报告成功。
            raise ContractError(Code.READBACK)
        if reserved.outcome != Outcome.VERIFIED:
            return original
        self._ensure_current_stage(reserved.loan)
        return original

    def _finish_pending_reservation(self, loan, original=None):
        """借出行停在 reservation_pending：从日志补齐这一跳，而不是每轮只报重复事件。

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
          `reservation_pending` and the reserve event was never consumed, and
          the movement itself only proves what the read it was planned from
          shows: enough `available` for the quantity, plus the adapter's
          compare-before-write against that same read — a guard over the
          window between read and write, not a proof about history;
        * what history is checked against is the stock snapshot recorded in the
          approval receipt: the fresh stock read must still match it (adapter
          revision token aside). A hand-edited table — stock row already
          `reserved` while the loan row still says `reservation_pending` —
          fails that comparison, so this pass reports a blocked item and moves
          nothing instead of reserving the quantity a second time. Any stock
          movement between the approval write and this pass (a concurrent loan,
          say) is escalated to a human for the same reason.

        Unprovable evidence is reported as a blocked item, never retried, and
        never guessed at.
        """
        current = self.engine.reader.read_loan(loan.ref)
        if current.state != State.RESERVATION_PENDING:
            raise _Pending(Code.STATE)
        record = self._recorded_approval(current)
        if record is None:
            raise _Pending(Code.STATE)
        event, snapshot = record
        if snapshot is None:
            raise _Pending(Code.STATE)
        fresh = self.engine.reader.read_inventory(current.item)
        if replace(fresh, revision=snapshot.revision) != snapshot:
            # 库存行和审批回执记录的快照对不上：已经有人动过这张表，这一份预留
            # 到没到账无从证明，交给人工，别照着猜再迁一次。
            raise _Pending(Code.CONFLICT)
        return self._reserve(current, event, original)

    def _recorded_approval(self, loan):
        """The applied approval for this loan plus its recorded stock snapshot.

        Only a readback-verified approval write whose event the ledger already
        consumed qualifies; zero or several candidates fail closed. The queue
        row that carried the entry is deliberately *not* part of the key: a
        hand-edited or re-typed row must not be able to hide the approval write
        that did land, and the loan ref + ``action == APPROVE`` + consumed +
        ``VERIFIED`` + exactly-one-candidate conditions already make the match
        unique. Returns ``(event, snapshot)`` or ``None``.
        """
        found = []
        for operation_id in self.store.ids():
            intent, receipt = self.store.load(operation_id)
            if isinstance(intent, StageRequest) or intent.event.action != Action.APPROVE:
                continue
            if intent.before.ref != loan.ref:
                continue
            if receipt is None or receipt.outcome != Outcome.VERIFIED:
                continue
            if intent.event.event_id not in loan.consumed_events:
                continue
            found.append((intent.event, receipt.inventory))
        return found[0] if len(found) == 1 else None

    def _known_loans(self, extra=()):
        """Loan refs the queue or the journal already mentions, first-seen order.

        Both journal entry kinds count: a verified stage receipt names the loan
        whose entry was already built, and a write intent names the loan a
        ledger write addressed, whatever its receipt says. Reading only verified
        stage receipts missed every loan whose sole trace is a write intent —
        the local index dropping that one record is enough — and such a loan
        never got its entry point rebuilt.

        Applications discovered this pass count too: their ledger row is brand
        new, so the reconcile is what gives it a human entry point even if the
        `apply` item itself is skipped later in the same pass.
        """
        refs = []
        seen = set()
        for item in self._work(extra):
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

    def _reconcile_stages(self, extra=()):
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
        for ref in self._known_loans(extra):
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


def title_display_from_document(document):
    """绑定里可选的 ``title_display`` 段（issue #76）。没配就返回 None。

    这一段只影响待办标题好不好读，不参与任何结论：字段 ID 与表单链接都是本机值，
    不进仓库；一个键都没给（或全是空串）时返回 None，标题保持之前的样子。
    """
    from integrations.dingtalk.adapter import TitleDisplay

    raw = document.get('title_display')
    if raw is None:
        return None
    require(isinstance(raw, dict), Code.CONFIG)
    item_name_field = raw.get('item_name_field', '')
    borrower_names = raw.get('borrower_names', False)
    approve_entry_url = raw.get('approve_entry_url', '')
    require(isinstance(item_name_field, str) and isinstance(approve_entry_url, str),
            Code.CONFIG)
    require(type(borrower_names) is bool, Code.CONFIG)
    display = TitleDisplay(item_name_field, borrower_names, approve_entry_url)
    if not display.names_enabled and not display.approve_entry_url.strip():
        return None
    return display


def live_adapter(runtime, journal, locks, document, fields=None, entry_fields=None,
                 apply_fields=None, application_container=None,
                 return_form_fields=None, loan_container=None,
                 title_display=None):
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
    if title_display is None:
        title_display = title_display_from_document(document)
    transport = DwsTransport(
        cmd, fields, form_container=form_container, todo_container=todo_container,
        work_dir=runtime, entry_fields=entry_fields,
    )
    return DingTalkAdapter(
        transport, journal, locks, fields, entry_fields,
        apply_fields=apply_fields, application_container=application_container,
        return_form_fields=return_form_fields, entry_container=form_container,
        loan_container=loan_container, title_display=title_display,
    )


def application_intake(engine, reader, sources, store, locks, runtime, document=None):
    """Discovery port when the injected reader can scan applications, else None.

    Feature-detected on purpose: an injected port that has no application read
    side (a query-only double, a mirror reader) keeps the drive exactly as it
    was, and a live adapter always has both halves. The enable water mark comes
    from the binding document (``application_intake.since``); unset means the
    discovery only reports and never writes.
    """
    if not (hasattr(reader, 'pending_applications') and hasattr(reader, 'read_application')
            and hasattr(reader, 'create_application_loan')
            and hasattr(reader, 'find_application_loan')):
        return None
    return ApplicationIntake(engine, reader, sources, store, locks,
                             Path(runtime) / REGISTER_NAME,
                             since=intake_since_from_document(document or {}))


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
        return DriveLoop(engine, sources, store, locks,
                         intake=application_intake(engine, reader, sources, store, locks,
                                                   runtime, document)).run()
    finally:
        engine.stop()


def dump_sources(path, items):
    payload = [{'kind': item.kind, 'loan': encode_resource(item.loan_ref),
                'source': encode_resource(item.source)} for item in items]
    path = Path(path)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + '\n',
                    encoding='utf-8')
