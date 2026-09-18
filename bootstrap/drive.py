"""Non-interactive lending driver: recover journal, then consume trusted sources.

Does not modify workflow/ or contracts/. The engine and ports are injected.
Live DingTalk wiring belongs to T07 (dws_cmd must be supplied; this module
never guesses an install path). Ledger and stage-entry field maps both come
from binding.json; neither falls back to synthetic IDs.

A crash between the approval write and the system reservation leaves the loan
in `reservation_pending` with the approval event already consumed, so every
later pass used to report nothing but DUPLICATE_EVENT. See
`_finish_pending_reservation`.

Anything this module cannot prove is reported as a 挂起 (``Hang`` code plus a
readable Chinese reason), never as a silent success: an unresolved write that
the requery could not settle counts as a hang, not as a recovered pass.
`drive_exit_code` turns a pass that has hangs (or skips a human has to look at)
into a non-zero exit code for the scheduler, without ever aborting the pass.
"""
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from contracts.flow import Outcome
from contracts.model import Action, Code, ContractError, State, require
from contracts.ports import StageRequest, check_binding, stage_operation_id
from workflow.engine import LendingEngine

from .binding import (binding_from_document, field_maps_from_document,
                      intake_since_from_document, read_binding_document,
                      require_complete, return_intake_since_from_document)
from .gate import assert_business_allowed
from .inbox import (REGISTER_NAME, ApplicationIntake, IntakeReport, IntakeSkip,
                    format_intake_lines)
from .instance import MachineLock
from .journal import FileJournal, is_resolved
from .returns import ReturnIntake, failed_return_report, format_return_lines
from .snapshot import decode_resource, encode_resource

_NEXT_STAGE = {
    State.AWAITING_APPROVAL: Action.APPROVE,
    State.AWAITING_ISSUE: Action.ISSUE,
    State.BORROWED: Action.REQUEST_RETURN,
    State.AWAITING_RETURN: Action.RETURN,
}


class Hang(StrEnum):
    """Why this pass gave up on a loan: a driver-side reason, not a contract code.

    挂起项的码要说得出「为什么挂」。合同拒绝码（``Code``）不够用：同一个
    ``INVALID_STATE`` 会盖住「翻不到审批证据」「单已经被别人推进」「库存快照对不上」
    三件完全不同的事，报告里看不出该找谁、该做什么。所以驱动侧的挂起一律用这里的码，
    再配一句人话（``HANG_NOTES``），摘要、明细行与「待人工」提示都会带上它。
    """

    APPROVAL_UNPROVEN = 'HANG_APPROVAL_UNPROVEN'
    APPROVAL_AMBIGUOUS = 'HANG_APPROVAL_AMBIGUOUS'
    SNAPSHOT_MISSING = 'HANG_SNAPSHOT_MISSING'
    STOCK_MOVED = 'HANG_STOCK_MOVED'
    LOAN_MOVED = 'HANG_LOAN_MOVED'
    UNRESOLVED_WRITE = 'HANG_UNRESOLVED_WRITE'


HANG_NOTES = {
    Hang.APPROVAL_UNPROVEN: '审批已写、预留未写，本机翻不到那条已落库且被日志消费的审批证据'
                            '（应在写审批的那台机器上跑一轮，或人工补预留）',
    Hang.APPROVAL_AMBIGUOUS: '同一单翻到多条候选审批，不挑一条（挑哪条都会换掉操作号），'
                             '需人工清理重复审批行',
    Hang.SNAPSHOT_MISSING: '审批回执没记库存快照，预留该按哪个基线比对无从证明，需人工确认',
    Hang.STOCK_MOVED: '库存行与审批回执记录的快照对不上（被并发动用或手改过），'
                      '这份预留到没到账无从证明，别照着猜再迁一次',
    Hang.LOAN_MOVED: '这一行落到待预留补齐，但单已经不在待预留状态（已被推进或改表），'
                     '这一行的证据不是判据',
    Hang.UNRESOLVED_WRITE: '日志里这次写回查后仍没有结论，待人工上平台查这次写到底落没落',
}


class _Pending(ContractError):
    """Half-finished transition the driver must neither retry nor hide.

    Raised only where the ledger is between two external writes and the driver
    cannot prove enough to finish the step. run() reports it in `blocked`, so
    the loan stays visible to a human instead of looking like a plain
    duplicate event on every pass.

    ``code`` 是驱动侧的挂起原因码（``Hang``）：同一个合同码盖住多种原因时，报告里
    看不出为什么挂。``note`` 是给操作员看的那句话，缺省取 ``HANG_NOTES`` 里该码的
    说明，摘要与明细行都会带上。
    """

    def __init__(self, code, note=None):
        super().__init__(code)
        self.note = HANG_NOTES.get(code.value, '') if note is None else note


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
    note: str = ''


@dataclass(frozen=True)
class DriveReport:
    """One pass, with stage reconciliation kept in its own account.

    ``recovered`` is only the unresolved-journal recovery, and only the
    operations that really settled (``is_resolved``): the unresolved-journal
    count must never include an operation the requery could not conclude, and
    the entry points the reconcile created are writes, so they are reported by
    ``stage_created`` (operation ids) and never mixed into the recovery count.
    ``stage_checked`` holds the known loan refs the reconcile examined.
    Reconcile skips travel in ``skipped`` like every other skip, tagged
    ``kind='stage'``.

    A requery that came back without a conclusion is a 挂起: it lands in
    ``blocked`` as ``Hang.UNRESOLVED_WRITE`` with the loan and source it
    concerns, so the report never says "已回查" and "挂起" about the same
    operation at once.

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

    ``returns`` is the same account for return-form rows (issue #87), kept
    **apart** from ``intake`` on purpose: the two lines scan two different
    tables, carry their own water marks and their own skips, and one line's
    counts must never overwrite the other's. Both feed the same queue, so a
    registered return travels the same path as a hand-registered one.
    """

    recovered: tuple
    processed: tuple
    skipped: tuple
    blocked: tuple
    stage_checked: tuple = ()
    stage_created: tuple = ()
    healed: tuple = ()
    intake: object = None
    returns: object = None


def _failed_intake(code, note='发现扫描抛异常，本轮未登记'):
    """发现阶段没跑完的账目：本轮不登记任何申请，但这一轮照常继续。

    ``scan_code`` 非空即「查表没结论」，报告里与「扫到 0 行、表真的空」区分开。
    """
    return IntakeReport(scan_code=code, scan_note=note,
                        skipped=(IntakeSkip('-', code),))


def _drive_outcome(item, code, note=''):
    return DriveOutcome(
        kind=item.kind,
        loan_id=item.loan_ref.resource_id,
        source_kind=item.source.kind,
        source_id=item.source.resource_id,
        code=code,
        note=note,
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


def _open_stage_todos(outcomes):
    """Skips that really mean "the human has not acted yet".

    An open stage todo reads as ``STATE`` from the todo channel (no completion
    event yet) — the reader marks that case separately on purpose, because a
    *completed* todo whose evidence is unreadable still comes back as
    ``EVIDENCE``. Only the former is "等人工"; calling a broken completion
    "证据不足" would be wrong the other way round.
    """
    return tuple(o for o in outcomes
                 if o.code == Code.STATE.value and o.source_kind == 'todo')


def _waiting_on_human(report):
    """这一轮卡在「等人」上的两类条目：开着的阶段待办 + 挂起。

    旧口径只看 ``skipped``，最需要人的挂起（半写、证据不足、写入未决）反而不在
    「待人工」清单里。两类仍然分开列，因为处置不同：阶段待办没完成是**正常等待**
    （人去点一下），挂起是**要人查/补**（上面的 reason code 与说明已经写明是哪一种），
    混成一句话就分不出故障。
    """
    return _open_stage_todos(report.skipped), tuple(report.blocked)


def _hang_note_parts(hangs):
    """挂起按原因码分组：``码×条数（为什么挂）``，同一码只写一次说明。"""
    grouped = {}
    for outcome in hangs:
        grouped.setdefault(outcome.code, []).append(outcome)
    parts = []
    for code, items in sorted(grouped.items()):
        note = items[0].note or HANG_NOTES.get(code, '')
        parts.append(f'{code}×{len(items)}' + (f'（{note}）' if note else ''))
    return parts


def _waiting_line(report):
    """「待人工」一行：阶段待办未完成 + 挂起，两类都点名。"""
    todos, hangs = _waiting_on_human(report)
    if not todos and not hangs:
        return None
    parts = []
    if todos:
        parts.append('阶段待办未完成 {count} 条（{ids}）'.format(
            count=len(todos), ids='，'.join(o.source_id for o in todos)))
    if hangs:
        parts.append('挂起 {count} 条（要人查/补，明细见下面挂起行：{why}）'.format(
            count=len(hangs), why='；'.join(_hang_note_parts(hangs))))
    return '  待人工 {total} 条：{parts}'.format(total=len(todos) + len(hangs),
                                                parts='；'.join(parts))


def _stage_skips(outcomes):
    """Reconcile skips share the skip tuple with queue skips, tagged by kind."""
    return tuple(o for o in outcomes if o.kind == 'stage')


def format_drive_lines(report):
    waiting = _waiting_line(report)
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
        lines.append(waiting)
    if report.healed:
        lines.append(
            '  自愈补齐 {summary}，按已落库审批补写预留'.format(
                summary=format_outcome_summary(report.healed))
        )
    if report.intake is not None:
        lines.extend(format_intake_lines(report.intake))
    if getattr(report, 'returns', None) is not None:
        # 归还发现另起一行：两条线的扫描/水位/登记/跳过各自算账，谁都不冲掉谁。
        lines.extend(format_return_lines(report.returns))
    for label, outcomes in (('跳过', report.skipped), ('挂起', report.blocked)):
        for outcome in outcomes:
            line = (f'  {label} {outcome.kind} loan={outcome.loan_id} '
                    f'{outcome.source_kind}={outcome.source_id} {outcome.code}')
            if outcome.note:
                line += f' {outcome.note}'
            lines.append(line)
    return lines


BENIGN_SKIP_CODES = frozenset({Code.DUPLICATE.value, Code.STATE.value})


def drive_exit_code(report):
    """这一轮有没有需要人接手的东西：0 = 没有，1 = 有。

    调度层（定时脚本）只看得见退出码，旧实现恒返回 0，挂起再多也不报警。口径与报告
    一致，只回答「要不要人看」，不改变这一轮的处置：

    * ``blocked`` 非空 —— 挂起（半写、证据不足、写入未决）都只能由人推进；
    * ``skipped`` 里出现非稳态的码 —— ``DUPLICATE_EVENT``（这行早就被消费过）与
      ``INVALID_STATE``（当前状态没有下一步人工动作，或阶段待办还没人点）算日常
      稳态；其余（``EVIDENCE_REQUIRED``、``CONFIG_RECONFIRM_REQUIRED``、
      ``READBACK_MISMATCH``、``WRONG_PERSON``、``QUANTITY_MISMATCH`` …）都算
      失败，要人看；
    * 发现扫描本身没结论（``scan_code``）—— 缺表不等于空表，同样是失败。申请发现与
      归还发现两条线各自算：任一条没结论都算要人看。

    单个失败照旧只记一行、不中断整轮：退出码是这一轮跑完后的汇总，不是中断信号。
    """
    if report.blocked:
        return 1
    if any(outcome.code not in BENIGN_SKIP_CODES for outcome in report.skipped):
        return 1
    if getattr(report.intake, 'scan_code', ''):
        return 1
    if getattr(getattr(report, 'returns', None), 'scan_code', ''):
        return 1
    return 0


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

    def __init__(self, engine, sources, store, locks, intake=None, returns=None):
        self.engine = engine
        self.sources = sources
        self.store = store
        self.locks = locks
        self.intake = intake
        self.returns = returns

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

        ``blocked`` also carries the requery hangs from ``_recover``: an
        unresolved write that this pass could not settle is a human's problem,
        not a recovered operation.
        """
        assert_business_allowed(self.engine.binding, self.engine.lease, self.locks)
        processed = []
        skipped = []
        blocked = []
        # 申请发现先于队列消费：真人新提交的行必须在**同一轮**里变成审批入口，
        # 而不是等操作员登记。发现本身也只写本机队列与台账行，闸门照旧。
        reported = self._run_intake()
        # 归还发现紧随其后，同样先于队列消费：真人填完归还表单那一行也要在同一轮里
        # 变成待归还确认，而不是等操作员手工往队列里加一条引用（#87）。
        returned = self._run_return_intake()
        discovered = tuple(WorkItem(finding.kind, finding.loan_ref, finding.source)
                           for finding in (getattr(reported, 'findings', ())
                                           + getattr(returned, 'findings', ())))
        healed = []
        recovered, hangs = self._recover()
        stage_checked, stages_created, stage_skips = self._reconcile_stages(discovered)
        skipped.extend(stage_skips)
        # 回查没结清的排在最前：它们是这一轮最先被看见的问题，明细行自带原因说明。
        blocked.extend(hangs)
        blocked_loans = set(self._unresolved_loans())
        for item in self._work(discovered):
            if item.loan_ref in blocked_loans:
                blocked.append(_drive_outcome(item, Code.UNKNOWN.value))
                continue
            try:
                explanation = self._handle(item)
            except _Pending as exc:
                blocked.append(_drive_outcome(item, exc.code.value, exc.note))
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
                           tuple(healed), reported, returned)

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

    def _run_return_intake(self):
        """这一轮的归还发现账目；没接归还发现就是 None。

        与申请发现同一口径：发现是**便利**，不是这一轮本身 —— 只有实例闸门
        （``SECOND_INSTANCE_BLOCKED``）才允许带着整轮一起停，其余任何异常都记进归还
        发现账目（``scan_code``）并按「本轮未登记」继续。
        """
        if self.returns is None:
            return None
        try:
            return self.returns.run()
        except ContractError as exc:
            if exc.code == Code.INSTANCE:
                raise
            return failed_return_report(exc.code.value, note='归还发现扫描未完成，本轮未登记')
        except Exception:
            return failed_return_report(Code.UNKNOWN.value)

    def _recover(self):
        """回查未决流水：只有真的结清的才算「回查」，查不出结论的落成挂起。

        ``resolve`` 无论查没查出结论都会返回，旧行为把每条都记进 ``recovered``，
        半写场景于是同一张单同时印成「回查 1」和「挂起 1」（那张单其实永远结不清）。
        这里只看一件事：这一轮过后，这条流水还在不在未决清单里 —— 判据与
        ``FileJournal.unresolved_ids`` 同源（``is_resolved``）。还在的就是挂起，
        带上它是哪张单、哪个来源，以及回查给了什么答复。
        """
        recovered = []
        stuck = []
        for operation_id in self.store.unresolved_ids():
            assert_business_allowed(self.engine.binding, self.engine.lease, self.locks)
            resolution = self.engine.resolve(operation_id)
            _, receipt = self.store.load(operation_id)
            if is_resolved(receipt):
                recovered.append(operation_id)
            else:
                stuck.append(self._stuck_outcome(operation_id, resolution))
        return tuple(recovered), tuple(stuck)

    def _stuck_outcome(self, operation_id, resolution):
        """没结清的那条流水在报告里长什么样：单 + 来源 + 回查的答复。"""
        intent, _ = self.store.load(operation_id)
        if isinstance(intent, StageRequest):
            loan_id = intent.loan.ref.resource_id
            source_kind, source_id = 'stage', operation_id
        else:
            loan_id = intent.before.ref.resource_id
            source_kind = intent.event.source.kind
            source_id = intent.event.source.resource_id
        outcome = getattr(resolution, 'outcome', Outcome.UNKNOWN)
        error = getattr(resolution, 'error', None) or Code.UNKNOWN
        note = ('回查没有结论（回执 {outcome}，答复码 {code}），流水 {operation_id} 不算已回查：'
                '下一轮继续查；长期如此需人工上平台查这次写到底落没落').format(
                    outcome=outcome, code=error, operation_id=operation_id)
        return DriveOutcome('recover', loan_id, source_kind, source_id,
                            Hang.UNRESOLVED_WRITE.value, note)

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

        挂起的码要说得出为什么挂：翻不到审批证据、多条候选、没有快照、库存已被动过、
        单已经不是待预留，各用一个驱动侧的 ``Hang`` 码，报告里一眼能分辨（旧行为一律
        ``INVALID_STATE``）。
        """
        current = self.engine.reader.read_loan(loan.ref)
        if current.state != State.RESERVATION_PENDING:
            raise _Pending(Hang.LOAN_MOVED)
        record, reason = self._recorded_approval(current)
        if record is None:
            raise _Pending(reason)
        event, snapshot = record
        if snapshot is None:
            raise _Pending(Hang.SNAPSHOT_MISSING)
        fresh = self.engine.reader.read_inventory(current.item)
        if replace(fresh, revision=snapshot.revision) != snapshot:
            # 库存行和审批回执记录的快照对不上：已经有人动过这张表，这一份预留
            # 到没到账无从证明，交给人工，别照着猜再迁一次。
            raise _Pending(Hang.STOCK_MOVED)
        return self._reserve(current, event, original)

    def _recorded_approval(self, loan):
        """The applied approval for this loan plus its recorded stock snapshot.

        Only a readback-verified approval write whose event the ledger already
        consumed qualifies; zero or several candidates fail closed. The queue
        row that carried the entry is deliberately *not* part of the key: a
        hand-edited or re-typed row must not be able to hide the approval write
        that did land, and the loan ref + ``action == APPROVE`` + consumed +
        ``VERIFIED`` + exactly-one-candidate conditions already make the match
        unique. Returns ``(event, snapshot)`` or ``(None, hang_code)`` —— 没翻到
        与翻到多条是两件不同的事，挂起码分开，免得报告把「证据不足」说成一种。
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
        if len(found) == 1:
            return found[0], None
        return None, (Hang.APPROVAL_AMBIGUOUS if found else Hang.APPROVAL_UNPROVEN)

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


def return_intake(engine, reader, sources, store, locks, document=None):
    """归还发现的端口：注入的读侧能扫归还表时才接上，否则 None（照旧）。

    与 ``application_intake`` 同一做法，两处刻意的保守选择：

    * **特性探测**：没有归还读侧的端口（只读查询替身、镜像读侧、没声明归还表单的旧实例）
      保持驱动原样。真机适配器两半都有，且要求的 ``entry_container`` 在活绑定里是必填。
    * **水位**：取绑定里的 ``return_intake.since``；未配时发现只出报告、不登记一行。
    """
    if not (hasattr(reader, 'pending_returns') and hasattr(reader, 'read_return')
            and hasattr(reader, 'resolve_return_form_loan')):
        return None
    if not getattr(reader, 'entry_container', ''):
        return None
    return ReturnIntake(engine, reader, sources, store, locks,
                        since=return_intake_since_from_document(document or {}))


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
                                                   runtime, document),
                         returns=return_intake(engine, reader, sources, store, locks,
                                               document)).run()
    finally:
        engine.stop()


def dump_sources(path, items):
    payload = [{'kind': item.kind, 'loan': encode_resource(item.loan_ref),
                'source': encode_resource(item.source)} for item in items]
    path = Path(path)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + '\n',
                    encoding='utf-8')
