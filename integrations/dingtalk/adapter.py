"""DingTalk Read/Write/Stage ports over an injected transport.

No credentials, no real platform calls. Tests inject a fake transport.
Unknown write results stay unknown until an exact query; the adapter never
blindly resends.
"""
from dataclasses import replace

from contracts.flow import Receipt, verify
from contracts.model import (Action, Code, ContractError, Event, Identity,
                             IdentityBinding, Outcome, Resource, require, text)
from contracts.ports import StageReceipt, StageRequest, check_binding, verify_stage

from .cells import read_datetime, read_single_select, read_text
from .codec import (_identity, _int_count, _text_list, decode_inventory,
                    decode_loan, encode_inventory, encode_loan)
from .envelope import extract_records, record_cells, record_id
from .errors import (BusinessErrorResponse, DingTalkShapeError,
                     UnknownResultError)
from .identity import TODO
from .todo import completion_events, finish_time, read_todo_detail
from .transport import require_envelope


def _closed(exc, code=Code.EVIDENCE):
    raise ContractError(code) from exc


def _business_code(exc):
    if exc.code in ('FORBIDDEN', 'PERMISSION_DENIED'):
        return Code.IDENTITY
    return Code.UNKNOWN


_FORM_DECISIONS = {
    'agree': Action.APPROVE,
    'reject': Action.REJECT,
    'cancel': Action.CANCEL,
    'return': Action.REQUEST_RETURN,
}


class DingTalkAdapter:
    """Implements ReadPort, WritePort and StagePort against `transport`."""

    def __init__(self, transport, journal, leases, fields):
        self.transport = transport
        self.journal = journal
        self.leases = leases
        self.fields = fields

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
            if saved.outcome == Outcome.UNKNOWN:
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
        if verify(intent, receipt).outcome != Outcome.VERIFIED:
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

    def _read_form_event(self, loan, source):
        cells = self._record_cells(source)
        try:
            require(read_text(cells, self.fields.loan_container) == loan.ref.container_id,
                    Code.WRONG_LOAN)
            require(read_text(cells, self.fields.loan_id) == loan.ref.resource_id,
                    Code.WRONG_LOAN)
            require(read_text(cells, self.fields.config_version) == loan.config_version,
                    Code.CONFIG)
            action = _FORM_DECISIONS[read_single_select(cells, self.fields.decision).id]
            if action == Action.REQUEST_RETURN:
                actor = _identity(cells, self.fields.borrower, loan.ref.tenant_id)
            elif action == Action.CANCEL:
                actor = _identity(cells, self.fields.manager, loan.ref.tenant_id)
            else:
                actor = _identity(cells, self.fields.approver, loan.ref.tenant_id)
            occurred = read_datetime(cells, self.fields.occurred_at)
            return_ref = None
            quantity = None
            physical_ids = ()
            if action == Action.REQUEST_RETURN:
                quantity = _int_count(cells, self.fields.quantity, zero=False)
                physical_ids = _text_list(cells, self.fields.physical_ids)
                return_ref = Resource(
                    'record', loan.ref.tenant_id,
                    read_text(cells, self.fields.return_container),
                    read_text(cells, self.fields.return_id),
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
            envelope = require_envelope(payload)
            detail = read_todo_detail(envelope)
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except DingTalkShapeError as exc:
            _closed(exc)
        events = completion_events(detail)
        require(bool(events), Code.EVIDENCE)
        actors = {item.actor.value for item in events}
        require(len(actors) == 1, Code.EVIDENCE)
        actor_ref = events[0].actor
        actor_ref.require(TODO)
        require(finish_time(detail) is not None, Code.EVIDENCE)
        result = envelope.get('result')
        require(isinstance(result, dict) and 'occurredAt' in result, Code.EVIDENCE)
        try:
            occurred = read_datetime({'t': result['occurredAt']}, 't')
        except DingTalkShapeError as exc:
            _closed(exc)
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
        require(binding.internal.user_id == actor_ref.value, Code.WRONG_PERSON)
        action = Action(queried['action'])
        require(action in (Action.ISSUE, Action.RETURN), Code.STATE)
        return Event(
            action, f'{source.resource_id}:completion', loan.ref, source,
            binding.internal, occurred, loan.config_version,
            'todo_completion', True, binding=binding,
            evidence_ref=f'todo:{source.resource_id}:completion',
        )
