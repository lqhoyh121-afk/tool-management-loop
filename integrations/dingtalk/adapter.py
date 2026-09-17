"""DingTalk Read/Write/Stage ports over an injected transport.

No credentials, no real platform calls. Tests inject a fake transport.
Unknown write results stay unknown until an exact query. A query that
finds both loan and stock still at the pre-write snapshot is NOT_SENT and
may be retried. Partial writes stay unknown and are not replayed.
"""
from dataclasses import replace

from contracts.flow import Receipt, verify
from contracts.model import (Action, Code, ContractError, Event, Identity,
                             IdentityBinding, Outcome, Resource, State, require, text)
from contracts.ports import StageReceipt, StageRequest, check_binding, verify_stage

from .cells import (MissingFieldError, read_datetime, read_single_select,
                    read_text)
from .codec import (_identity, _int_count, _text_list, decode_inventory,
                    decode_loan, encode_inventory, encode_loan)
from .envelope import extract_records, record_cells, record_id
from .errors import (BusinessErrorResponse, DingTalkShapeError,
                     UnknownResultError)
from .identity import TODO
from .todo import (completion_at, completion_events, executor_refs, finish_time,
                   read_todo_detail)
from .transport import require_envelope, require_todo_envelope


def _closed(exc, code=Code.EVIDENCE):
    raise ContractError(code) from exc


def _business_code(exc):
    if exc.code in ('FORBIDDEN', 'PERMISSION_DENIED'):
        return Code.IDENTITY
    return Code.UNKNOWN


_FORM_DECISIONS = {
    'apply': Action.APPLY,
    'agree': Action.APPROVE,
    '同意': Action.APPROVE,
    'reject': Action.REJECT,
    '拒绝': Action.REJECT,
    'cancel': Action.CANCEL,
    'return': Action.REQUEST_RETURN,
    'request_return': Action.REQUEST_RETURN,
    '归还': Action.REQUEST_RETURN,
}


def _form_action(name):
    try:
        return _FORM_DECISIONS[name]
    except KeyError as exc:
        raise ContractError(Code.EVIDENCE) from exc


class DingTalkAdapter:
    """Implements ReadPort, WritePort and StagePort against `transport`."""

    def __init__(self, transport, journal, leases, fields, entry_fields,
                 apply_fields=None, application_container=None,
                 return_form_fields=None, entry_container=None, loan_container=None):
        self.transport = transport
        self.journal = journal
        self.leases = leases
        self.fields = fields
        self.entry_fields = entry_fields
        self.apply_fields = apply_fields
        self.application_container = application_container or ''
        self.return_form_fields = return_form_fields
        self.entry_container = entry_container or ''
        self.loan_container = loan_container or ''

    def read_loan(self, ref):
        return decode_loan(ref, self._record_cells(ref), self.fields)

    def read_inventory(self, ref):
        return decode_inventory(ref, self._record_cells(ref), self.fields)

    def read_event(self, loan, source):
        if source.kind == 'form':
            return self._read_form_event(loan, source)
        if source.kind == 'todo':
            return self._read_todo_event(loan, source)
        raise ContractError(Code.EVIDENCE)

    def submit(self, intent, binding, lease):
        self.leases.assert_held(lease)
        check_binding(binding, intent.before)
        self.journal.prepare(intent)
        _, saved = self.journal.load(intent.operation_id)
        if saved is not None:
            if saved.outcome == Outcome.VERIFIED:
                return saved
            if saved.outcome in (Outcome.UNKNOWN, Outcome.NOT_SENT):
                queried = self.query(intent)
                if queried.outcome == Outcome.VERIFIED:
                    return queried
                if queried.outcome != Outcome.NOT_SENT:
                    raise ContractError(Code.UNKNOWN)
        current = self.read_loan(intent.before.ref)
        stock = self.read_inventory(intent.stock_before.ref)
        require(current == intent.before, Code.CONFLICT)
        require(stock == intent.stock_before, Code.CONFLICT)
        self.journal.save_receipt(Receipt(intent.operation_id, Outcome.UNKNOWN))
        try:
            self._put_record(intent.after.ref, encode_loan(intent.after, self.fields))
            self._put_record(intent.stock_after.ref, encode_inventory(intent.stock_after, self.fields))
        except UnknownResultError:
            return Receipt(intent.operation_id, Outcome.UNKNOWN)
        except BusinessErrorResponse as exc:
            if _business_code(exc) is Code.IDENTITY:
                raise ContractError(Code.IDENTITY) from exc
            return Receipt(intent.operation_id, Outcome.UNKNOWN)
        return self.query(intent)

    def query(self, intent):
        try:
            loan = self.read_loan(intent.after.ref)
            inventory = self.read_inventory(intent.stock_after.ref)
        except (UnknownResultError, ContractError):
            receipt = Receipt(intent.operation_id, Outcome.UNKNOWN)
            self.journal.save_receipt(receipt)
            return receipt
        evidence = (
            f'read:{intent.after.ref.resource_id};'
            f'read:{intent.stock_after.ref.resource_id}'
        )
        receipt = Receipt(intent.operation_id, Outcome.VERIFIED, evidence, loan, inventory)
        checked = verify(intent, receipt).outcome
        if checked != Outcome.VERIFIED:
            if loan == intent.before and inventory == intent.stock_before:
                receipt = Receipt(intent.operation_id, Outcome.NOT_SENT, evidence, loan, inventory)
            else:
                receipt = Receipt(intent.operation_id, Outcome.UNKNOWN, evidence, loan, inventory)
        self.journal.save_receipt(receipt)
        return receipt

    def create_stage(self, request: StageRequest, binding, lease: str) -> StageReceipt:
        self.leases.assert_held(lease)
        check_binding(binding, request.loan)
        self.journal.prepare(request)
        _, saved = self.journal.load(request.operation_id)
        if saved is not None:
            if saved.outcome == Outcome.VERIFIED:
                return saved
            if saved.outcome == Outcome.UNKNOWN:
                return self.query_stage(request)
        self.journal.save_receipt(StageReceipt(request.operation_id, Outcome.UNKNOWN))
        command = 'todo.create' if request.action in (Action.ISSUE, Action.RETURN) else 'form.create'
        try:
            payload = self.transport.exchange(command, {
                'operation_id': request.operation_id,
                'tenant_id': request.loan.ref.tenant_id,
                'loan_container': request.loan.ref.container_id,
                'loan_id': request.loan.ref.resource_id,
                'item_container': request.loan.item.container_id,
                'item_id': request.loan.item.resource_id,
                'action': request.action.value,
                'actor': request.actor.user_id,
                'borrower': request.loan.borrower.user_id,
                'approver': request.loan.approver.user_id,
                'manager': request.loan.manager.user_id,
                'config_version': request.loan.config_version,
                'quantity': request.loan.quantity,
                'physical_ids': list(request.loan.physical_ids),
            })
            if command == 'todo.create':
                require_todo_envelope(payload)
            else:
                require_envelope(payload)
        except UnknownResultError:
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
            self.journal.save_receipt(receipt)
            return receipt
        except BusinessErrorResponse as exc:
            if _business_code(exc) is Code.IDENTITY:
                raise ContractError(Code.IDENTITY) from exc
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
            self.journal.save_receipt(receipt)
            return receipt
        except DingTalkShapeError:
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
            self.journal.save_receipt(receipt)
            return receipt
        return self.query_stage(request)

    def query_stage(self, request: StageRequest) -> StageReceipt:
        try:
            payload = self.transport.exchange('stage.query', {
                'operation_id': request.operation_id,
            })
            result = require_envelope(payload).get('result')
            require(isinstance(result, dict), Code.EVIDENCE)
            receipt = self._stage_from_query(request, result)
        except UnknownResultError:
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
        except (BusinessErrorResponse, DingTalkShapeError, ContractError, KeyError, TypeError, ValueError):
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
        self.journal.save_receipt(receipt)
        return receipt

    def _record_cells(self, ref):
        try:
            payload = self.transport.exchange('record.query', {
                'tenant_id': ref.tenant_id,
                'container_id': ref.container_id,
                'resource_id': ref.resource_id,
            })
            records = extract_records(payload)
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except DingTalkShapeError as exc:
            _closed(exc)
        require(len(records) == 1, Code.EVIDENCE)
        record = records[0]
        try:
            require(record_id(record) == ref.resource_id, Code.WRONG_LOAN)
            return record_cells(record)
        except DingTalkShapeError as exc:
            _closed(exc)

    def _put_record(self, ref, cells):
        payload = self.transport.exchange('record.update', {
            'tenant_id': ref.tenant_id,
            'container_id': ref.container_id,
            'resource_id': ref.resource_id,
            'cells': cells,
        })
        require_envelope(payload)

    def _stage_from_query(self, request, result):
        kind = result.get('kind')
        resource_id = result.get('resource_id')
        require(isinstance(resource_id, str) and resource_id, Code.EVIDENCE)
        container = result.get('container')
        require(isinstance(container, str) and container, Code.EVIDENCE)
        created = str(result.get('creation_evidence') or '')
        readback = str(result.get('readback_evidence') or '')
        if kind == 'form':
            source = Resource('form', request.loan.ref.tenant_id, container, resource_id)
            receipt = StageReceipt(request.operation_id, Outcome.VERIFIED, source,
                                   creation_evidence=created, readback_evidence=readback)
            return replace(receipt, outcome=verify_stage(request, receipt))
        if kind == 'todo':
            source = Resource('todo', request.loan.ref.tenant_id, container, resource_id)
            binding = self._binding_from_stage(request.loan, source, result)
            require(binding.contact == request.actor, Code.WRONG_PERSON)
            receipt = StageReceipt(request.operation_id, Outcome.VERIFIED, source, binding,
                                   created, readback)
            return replace(receipt, outcome=verify_stage(request, receipt))
        raise ContractError(Code.EVIDENCE)

    def _binding_from_stage(self, loan, source, queried):
        require(queried.get('kind') == 'todo', Code.EVIDENCE)
        require(queried.get('resource_id') == source.resource_id, Code.EVIDENCE)
        require(queried.get('loan_id') == loan.ref.resource_id, Code.WRONG_LOAN)
        require(queried.get('loan_container') == loan.ref.container_id, Code.WRONG_LOAN)
        require(queried.get('config_version') == loan.config_version, Code.CONFIG)
        created = str(queried.get('creation_evidence') or '')
        readback = str(queried.get('readback_evidence') or '')
        text(created)
        text(readback)
        raw = queried.get('internal_id')
        if isinstance(raw, bool) or raw is None:
            raise ContractError(Code.EVIDENCE)
        if isinstance(raw, int):
            internal_id = format(raw, 'd')
        elif isinstance(raw, str):
            internal_id = raw
        else:
            raise ContractError(Code.EVIDENCE)
        contact = Identity('contact', loan.ref.tenant_id, str(queried['contact']))
        internal = Identity('todo', contact.tenant_id, internal_id)
        return IdentityBinding(contact, internal, source, created, readback)

    def resolve_return_form_loan(self, source, hint_ref):
        """Return the sole borrowed loan for a minimal return-form row, else None."""
        if source.kind != 'form' or self.return_form_fields is None:
            return None
        cells = self._record_cells(source)
        if not self._is_return_form_submission(cells, source):
            return None
        actor = _identity(cells, self.return_form_fields.borrower, source.tenant_id)
        loan_container = self.loan_container or hint_ref.container_id
        text(loan_container)
        matches = self._borrowed_loan_ids(source.tenant_id, loan_container, actor.user_id)
        require(len(matches) == 1, Code.EVIDENCE)
        matched = Resource('record', source.tenant_id, loan_container, matches[0])
        return matched

    def _cell_has_text(self, cells, field_id):
        if field_id not in cells or cells[field_id] is None:
            return False
        try:
            return bool(read_text(cells, field_id).strip())
        except DingTalkShapeError:
            return False

    def _is_return_form_submission(self, cells, source):
        if self.return_form_fields is None:
            return False
        if self.entry_container and source.container_id != self.entry_container:
            return False
        entry = self.entry_fields
        if self._cell_has_text(cells, entry.loan_id):
            return False
        if self._cell_has_text(cells, entry.loan_container):
            return False
        fields = self.return_form_fields
        return (fields.borrower in cells and cells[fields.borrower] is not None
                and fields.occurred_at in cells and cells[fields.occurred_at] is not None)

    def _borrowed_loan_ids(self, tenant_id, loan_container, borrower_user_id):
        payload = self.transport.exchange('loan.query_borrowed', {
            'tenant_id': tenant_id,
            'loan_container': loan_container,
            'borrower': borrower_user_id,
        })
        result = require_envelope(payload).get('result')
        require(isinstance(result, dict), Code.EVIDENCE)
        loan_ids = result.get('loan_ids')
        require(isinstance(loan_ids, list), Code.EVIDENCE)
        cleaned = []
        for item in loan_ids:
            require(isinstance(item, str) and item.strip(), Code.EVIDENCE)
            cleaned.append(item)
        return cleaned

    def _read_form_event(self, loan, source):
        cells = self._record_cells(source)
        if (self.apply_fields is not None
                and source.container_id == self.application_container):
            return self._read_application_event(loan, source, cells)
        if self._is_return_form_submission(cells, source):
            return self._read_return_form_event(loan, source, cells)
        return self._read_entry_form_event(loan, source, cells)

    def _read_return_form_event(self, loan, source, cells):
        fields = self.return_form_fields
        try:
            actor = _identity(cells, fields.borrower, loan.ref.tenant_id)
            occurred = read_datetime(cells, fields.occurred_at)
            loan_container = self.loan_container or loan.ref.container_id
            matches = self._borrowed_loan_ids(source.tenant_id, loan_container, actor.user_id)
            require(len(matches) == 1, Code.EVIDENCE)
            require(matches[0] == loan.ref.resource_id, Code.WRONG_LOAN)
            require(actor == loan.borrower, Code.WRONG_PERSON)
            require(loan.state == State.BORROWED, Code.STATE)
            return_ref = Resource(
                'record', source.tenant_id, source.container_id, source.resource_id)
            require(return_ref != loan.ref, Code.WRONG_LOAN)
            action = Action.REQUEST_RETURN
        except ContractError:
            raise
        except DingTalkShapeError as exc:
            _closed(exc)
        return Event(
            action, f'{source.resource_id}:{action.value}', loan.ref, source,
            actor, occurred, loan.config_version, 'form', True,
            return_ref=return_ref, quantity=loan.quantity, physical_ids=loan.physical_ids,
            evidence_ref=f'form:{source.resource_id}',
        )

    def _read_application_event(self, loan, source, cells):
        fields = self.apply_fields
        try:
            action = Action.APPLY
            actor = _identity(cells, fields.borrower, loan.ref.tenant_id)
            occurred = read_datetime(cells, fields.occurred_at)
            quantity = _int_count(cells, fields.quantity, zero=False)
            if fields.physical_ids in cells and cells[fields.physical_ids] is not None:
                physical_ids = _text_list(cells, fields.physical_ids)
            else:
                physical_ids = ()
        except ContractError:
            raise
        except DingTalkShapeError as exc:
            _closed(exc)
        return Event(action, f'{source.resource_id}:{action.value}', loan.ref, source,
                     actor, occurred, loan.config_version, 'form', True,
                     quantity=quantity, physical_ids=physical_ids,
                     evidence_ref=f'form:{source.resource_id}')

    def _entry_form_action(self, cells, fields):
        if fields.decision in cells and cells[fields.decision] is not None:
            return _form_action(read_single_select(cells, fields.decision).name)
        if fields.action in cells and cells[fields.action] is not None:
            return _form_action(read_text(cells, fields.action))
        raise MissingFieldError('阶段入口缺少 decision 或 action')

    def _read_entry_form_event(self, loan, source, cells):
        fields = self.entry_fields
        try:
            require(read_text(cells, fields.loan_container) == loan.ref.container_id,
                    Code.WRONG_LOAN)
            require(read_text(cells, fields.loan_id) == loan.ref.resource_id,
                    Code.WRONG_LOAN)
            require(read_text(cells, fields.config_version) == loan.config_version,
                    Code.CONFIG)
            action = self._entry_form_action(cells, fields)
            if action in (Action.APPLY, Action.REQUEST_RETURN):
                actor = _identity(cells, fields.borrower, loan.ref.tenant_id)
            elif action == Action.CANCEL:
                actor = _identity(cells, fields.manager, loan.ref.tenant_id)
            else:
                actor = _identity(cells, fields.approver, loan.ref.tenant_id)
            if not self._cell_has_text(cells, fields.occurred_at):
                raise MissingFieldError(
                    '阶段入口缺少「发生时间」：决定与发生时间都要真人填，引擎不预填')
            occurred = read_datetime(cells, fields.occurred_at)
            return_ref = None
            quantity = None
            physical_ids = ()
            if action in (Action.APPLY, Action.REQUEST_RETURN):
                quantity = _int_count(cells, fields.quantity, zero=False)
                physical_ids = _text_list(cells, fields.physical_ids)
            if action == Action.REQUEST_RETURN:
                return_ref = Resource(
                    'record', loan.ref.tenant_id,
                    read_text(cells, fields.return_container),
                    read_text(cells, fields.return_id),
                )
        except ContractError:
            raise
        except (DingTalkShapeError, KeyError) as exc:
            _closed(exc)
        return Event(action, f'{source.resource_id}:{action.value}', loan.ref, source,
                     actor, occurred, loan.config_version, 'form', True,
                     return_ref=return_ref, quantity=quantity, physical_ids=physical_ids,
                     evidence_ref=f'form:{source.resource_id}')

    def _read_todo_event(self, loan, source):
        try:
            payload = self.transport.exchange('todo.get', {
                'tenant_id': source.tenant_id,
                'task_id': source.resource_id,
            })
            envelope = require_todo_envelope(payload)
            detail = read_todo_detail(envelope)
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except DingTalkShapeError as exc:
            _closed(exc, Code.UNKNOWN)
        events = completion_events(detail)
        # 待办还没被点完成：这是「等人工」，不是「证据不足」。驱动按本码区分，
        # 不要把未完成和「已完成但证据读不出来」混成一句提示。
        require(bool(events), Code.STATE)
        actors = {item.actor.value for item in events}
        require(len(actors) == 1, Code.EVIDENCE)
        actor_ref = events[0].actor
        actor_ref.require(TODO)
        try:
            require(finish_time(detail) is not None, Code.EVIDENCE)
            occurred = completion_at(detail)
        except DingTalkShapeError as exc:
            _closed(exc)
        require(occurred is not None, Code.EVIDENCE)
        try:
            meta_payload = self.transport.exchange('stage.query', {
                'task_id': source.resource_id,
            })
            queried = require_envelope(meta_payload).get('result')
            require(isinstance(queried, dict), Code.EVIDENCE)
            binding = self._binding_from_stage(loan, source, queried)
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except ContractError:
            raise
        except (DingTalkShapeError, KeyError, TypeError, ValueError) as exc:
            _closed(exc)
        executors = executor_refs(detail)
        require(executors, Code.EVIDENCE)
        require(actor_ref.same_person_as(executors[0]), Code.WRONG_PERSON)
        completer = Identity('todo', source.tenant_id, actor_ref.value)
        binding = replace(binding, internal=completer)
        action = Action(queried['action'])
        require(action in (Action.ISSUE, Action.RETURN), Code.STATE)
        return Event(
            action, f'{source.resource_id}:completion', loan.ref, source,
            completer, occurred, loan.config_version,
            'todo_completion', True, binding=binding,
            evidence_ref=f'todo:{source.resource_id}:completion',
        )
