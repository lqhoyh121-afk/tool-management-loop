"""Encode/decode frozen intents for the local operation journal.

Snapshots are recovery copies of the original intent, not a second inventory.
"""
from datetime import datetime

from contracts.flow import Receipt, WriteIntent
from contracts.model import (Action, Code, ContractError, Event, Identity, IdentityBinding,
                             Inventory, Loan, Outcome, Resource, State, require)
from contracts.ports import StageReceipt, StageRequest


def encode_entry(intent, receipt):
    return {
        'kind': _intent_kind(intent),
        'intent': _encode(intent),
        'receipt': None if receipt is None else _encode(receipt),
    }


def decode_entry(data):
    kind = data['kind']
    if kind == 'write_intent':
        intent = _write_intent(data['intent'])
    elif kind == 'stage_request':
        intent = _stage_request(data['intent'])
    else:
        raise KeyError(kind)
    raw = data.get('receipt')
    if raw is None:
        return intent, None
    if raw['kind'] == 'receipt':
        return intent, _receipt(raw)
    if raw['kind'] == 'stage_receipt':
        return intent, _stage_receipt(raw)
    raise KeyError(raw['kind'])


def encode_resource(resource):
    return {
        'kind': resource.kind,
        'tenant_id': resource.tenant_id,
        'container_id': resource.container_id,
        'resource_id': resource.resource_id,
    }


def decode_resource(data):
    return Resource(data['kind'], data['tenant_id'], data['container_id'], data['resource_id'])


def parse_loan_ref(raw, *, expected_container=None):
    """Parse a CLI loan target into a record ``Resource``.

    - ``tenant/container/record`` when ``container`` has no internal ``/``
    - ``tenant/base/table/record`` when the aitable container is ``base/table``

    When ``expected_container`` contains ``/`` but the parsed container does not,
    fail closed so a three-segment target cannot silently point at the wrong table.
    """
    parts = (raw or '').split('/')
    require(len(parts) in (3, 4) and all(part.strip() for part in parts), Code.CONFIG)
    if len(parts) == 3:
        ref = Resource('record', parts[0], parts[1], parts[2])
    else:
        ref = Resource('record', parts[0], f'{parts[1]}/{parts[2]}', parts[3])
    if (expected_container is not None
            and '/' in expected_container
            and ref.container_id != expected_container):
        raise ContractError(Code.CONFIG)
    return ref


def _intent_kind(intent):
    if isinstance(intent, WriteIntent):
        return 'write_intent'
    if isinstance(intent, StageRequest):
        return 'stage_request'
    raise TypeError(type(intent))


def _encode(value):
    if isinstance(value, WriteIntent):
        return {
            'kind': 'write_intent',
            'operation_id': value.operation_id,
            'before': _encode(value.before),
            'after': _encode(value.after),
            'stock_before': _encode(value.stock_before),
            'stock_after': _encode(value.stock_after),
            'event': _encode(value.event),
        }
    if isinstance(value, StageRequest):
        return {
            'kind': 'stage_request',
            'operation_id': value.operation_id,
            'loan': _encode(value.loan),
            'action': value.action.value,
            'actor': _encode(value.actor),
        }
    if isinstance(value, Receipt):
        return {
            'kind': 'receipt',
            'operation_id': value.operation_id,
            'outcome': value.outcome.value,
            'readback_evidence': value.readback_evidence,
            'loan': None if value.loan is None else _encode(value.loan),
            'inventory': None if value.inventory is None else _encode(value.inventory),
        }
    if isinstance(value, StageReceipt):
        return {
            'kind': 'stage_receipt',
            'operation_id': value.operation_id,
            'outcome': value.outcome.value,
            'source': None if value.source is None else encode_resource(value.source),
            'binding': None if value.binding is None else _encode(value.binding),
            'creation_evidence': value.creation_evidence,
            'readback_evidence': value.readback_evidence,
        }
    if isinstance(value, Loan):
        return {
            'kind': 'loan',
            'ref': encode_resource(value.ref),
            'item': encode_resource(value.item),
            'borrower': _encode(value.borrower),
            'approver': _encode(value.approver),
            'manager': _encode(value.manager),
            'quantity': value.quantity,
            'tracked': value.tracked,
            'physical_ids': list(value.physical_ids),
            'due_at': value.due_at.isoformat(),
            'config_version': value.config_version,
            'state': value.state.value,
            'return_ref': None if value.return_ref is None else encode_resource(value.return_ref),
            'consumed_events': list(value.consumed_events),
            'application_evidence': value.application_evidence,
        }
    if isinstance(value, Inventory):
        return {
            'kind': 'inventory',
            'ref': encode_resource(value.ref),
            'available': value.available,
            'reserved': value.reserved,
            'borrowed': value.borrowed,
            'available_ids': list(value.available_ids),
            'reserved_ids': list(value.reserved_ids),
            'borrowed_ids': list(value.borrowed_ids),
            'revision': value.revision,
        }
    if isinstance(value, Event):
        return {
            'kind': 'event',
            'action': value.action.value,
            'event_id': value.event_id,
            'loan_ref': encode_resource(value.loan_ref),
            'source': encode_resource(value.source),
            'actor': None if value.actor is None else _encode(value.actor),
            'occurred_at': value.occurred_at.isoformat(),
            'config_version': value.config_version,
            'evidence_kind': value.evidence_kind,
            'verified': value.verified,
            'binding': None if value.binding is None else _encode(value.binding),
            'return_ref': None if value.return_ref is None else encode_resource(value.return_ref),
            'quantity': value.quantity,
            'physical_ids': list(value.physical_ids),
            'evidence_ref': value.evidence_ref,
        }
    if isinstance(value, IdentityBinding):
        return {
            'kind': 'identity_binding',
            'contact': _encode(value.contact),
            'internal': _encode(value.internal),
            'task': encode_resource(value.task),
            'creation_evidence': value.creation_evidence,
            'readback_evidence': value.readback_evidence,
        }
    if isinstance(value, Identity):
        return {
            'kind': 'identity',
            'namespace': value.namespace,
            'tenant_id': value.tenant_id,
            'user_id': value.user_id,
        }
    if isinstance(value, Resource):
        return encode_resource(value)
    raise TypeError(type(value))


def _identity(data):
    return Identity(data['namespace'], data['tenant_id'], data['user_id'])


def _loan(data):
    return_ref = data.get('return_ref')
    return Loan(
        decode_resource(data['ref']),
        decode_resource(data['item']),
        _identity(data['borrower']),
        _identity(data['approver']),
        _identity(data['manager']),
        data['quantity'],
        data['tracked'],
        tuple(data['physical_ids']),
        datetime.fromisoformat(data['due_at']),
        data['config_version'],
        State(data['state']),
        None if return_ref is None else decode_resource(return_ref),
        tuple(data['consumed_events']),
        data['application_evidence'],
    )


def _inventory(data):
    return Inventory(
        decode_resource(data['ref']),
        data['available'],
        data['reserved'],
        data['borrowed'],
        tuple(data['available_ids']),
        tuple(data['reserved_ids']),
        tuple(data['borrowed_ids']),
        data['revision'],
    )


def _event(data):
    actor = data.get('actor')
    binding = data.get('binding')
    return_ref = data.get('return_ref')
    return Event(
        Action(data['action']),
        data['event_id'],
        decode_resource(data['loan_ref']),
        decode_resource(data['source']),
        None if actor is None else _identity(actor),
        datetime.fromisoformat(data['occurred_at']),
        data['config_version'],
        data['evidence_kind'],
        data['verified'],
        None if binding is None else IdentityBinding(
            _identity(binding['contact']),
            _identity(binding['internal']),
            decode_resource(binding['task']),
            binding['creation_evidence'],
            binding['readback_evidence'],
        ),
        None if return_ref is None else decode_resource(return_ref),
        data.get('quantity'),
        tuple(data.get('physical_ids') or ()),
        data.get('evidence_ref') or '',
    )


def _write_intent(data):
    return WriteIntent(
        data['operation_id'],
        _loan(data['before']),
        _loan(data['after']),
        _inventory(data['stock_before']),
        _inventory(data['stock_after']),
        _event(data['event']),
    )


def _stage_request(data):
    return StageRequest(
        data['operation_id'],
        _loan(data['loan']),
        Action(data['action']),
        _identity(data['actor']),
    )


def _receipt(data):
    loan = data.get('loan')
    inventory = data.get('inventory')
    return Receipt(
        data['operation_id'],
        Outcome(data['outcome']),
        data.get('readback_evidence') or '',
        None if loan is None else _loan(loan),
        None if inventory is None else _inventory(inventory),
    )


def _stage_receipt(data):
    source = data.get('source')
    binding = data.get('binding')
    return StageReceipt(
        data['operation_id'],
        Outcome(data['outcome']),
        None if source is None else decode_resource(source),
        None if binding is None else IdentityBinding(
            _identity(binding['contact']),
            _identity(binding['internal']),
            decode_resource(binding['task']),
            binding['creation_evidence'],
            binding['readback_evidence'],
        ),
        data.get('creation_evidence') or '',
        data.get('readback_evidence') or '',
    )
