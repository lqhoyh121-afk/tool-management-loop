"""Non-interactive lending driver: recover journal, then consume trusted sources.

Does not modify workflow/, contracts/, or integrations/dingtalk/. The engine
and ports are injected. Live DingTalk wiring belongs to T07 (dws_cmd must be
supplied; this module never guesses an install path).
"""
import json
from dataclasses import dataclass
from pathlib import Path

from contracts.flow import Outcome
from contracts.model import Action, Code, ContractError, State, require
from contracts.ports import StageRequest, check_binding
from workflow.engine import LendingEngine

from .binding import binding_path, document_from_files, load_binding, require_complete
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
class DriveReport:
    recovered: tuple
    processed: tuple
    skipped: tuple
    blocked: tuple


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
        recovered = self._recover()
        processed = []
        skipped = []
        blocked = []
        blocked_loans = set(self._unresolved_loans())
        for item in self._work():
            if item.loan_ref in blocked_loans:
                blocked.append(item)
                continue
            try:
                self._handle(item)
            except ContractError as exc:
                if exc.code == Code.INSTANCE:
                    raise
                if exc.code == Code.UNKNOWN:
                    blocked_loans.add(item.loan_ref)
                    blocked.append(item)
                    continue
                skipped.append((item, exc.code.value))
                continue
            processed.append(item)
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

    def _handle(self, item):
        loan = self.engine.reader.read_loan(item.loan_ref)
        check_binding(self.engine.binding, loan)
        self.locks.assert_held(self.engine.lease)
        if item.kind == 'apply':
            self.engine.admit_application(item.loan_ref, item.source)
            current = self.engine.reader.read_loan(item.loan_ref)
            self._ensure_current_stage(current)
            return
        execution = self.engine.execute(item.loan_ref, item.source)
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


def live_adapter(runtime, journal, locks, document):
    """Build DingTalkAdapter only from explicit binding fields. Never guess dws."""
    from integrations.dingtalk.adapter import DingTalkAdapter
    from integrations.dingtalk.dws_transport import DwsTransport
    from integrations.dingtalk.layout import FieldMap, SYNTHETIC_FIELDS

    cmd = document.get('dws_cmd')
    require(isinstance(cmd, list) and all(isinstance(part, str) and part for part in cmd),
            Code.CONFIG)
    form_container = document.get('form_container')
    todo_container = document.get('todo_container')
    require(isinstance(form_container, str) and form_container.strip(), Code.CONFIG)
    require(isinstance(todo_container, str) and todo_container.strip(), Code.CONFIG)
    fields = SYNTHETIC_FIELDS
    raw_fields = document.get('fields')
    if raw_fields is not None:
        require(isinstance(raw_fields, dict), Code.CONFIG)
        fields = FieldMap(**raw_fields)
    transport = DwsTransport(
        cmd, fields, form_container=form_container, todo_container=todo_container,
        work_dir=runtime,
    )
    return DingTalkAdapter(transport, journal, locks, fields)


def run_bound_drive(runtime, lock_root, reader=None, writer=None, stages=None,
                    sources=None, locks=None, store=None):
    runtime = Path(runtime)
    binding, _entry = load_binding(runtime)
    require_complete(binding)
    locks = locks or MachineLock(lock_root)
    store = store or FileJournal(runtime / 'operations')
    if reader is None or writer is None or stages is None:
        document = document_from_files(binding_path(runtime))
        adapter = live_adapter(runtime, store, locks, document)
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
