"""Record cell encode/decode using T01 observed cell types."""
import json

from contracts.model import (ContractError, Code, Identity, Inventory, Loan,
                             Resource, State, require)

from .cells import (read_creator, read_datetime, read_number, read_single_select,
                    read_text, read_text_or_empty)
from .errors import DingTalkShapeError
from .identity import RECORD_CREATOR


def _shape(exc):
    raise ContractError(Code.EVIDENCE) from exc


def _text_list(cells, field_id):
    raw = read_text(cells, field_id)
    if raw == '[]':
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(Code.EVIDENCE) from exc
    require(isinstance(parsed, list), Code.EVIDENCE)
    values = []
    for item in parsed:
        require(isinstance(item, str) and item.strip() == item and item, Code.EVIDENCE)
        values.append(item)
    return tuple(values)


def _int_count(cells, field_id, zero=True):
    parsed = read_number(cells, field_id)
    require(parsed == parsed.to_integral_value(), Code.QUANTITY)
    value = int(parsed)
    require(value >= (0 if zero else 1), Code.QUANTITY)
    return value


def _identity(cells, field_id, tenant_id):
    ref = read_creator(cells, field_id)
    ref.require(RECORD_CREATOR)
    require(ref.org == tenant_id, Code.IDENTITY)
    return Identity('contact', ref.org, ref.value)


def _put_identity(identity):
    require(identity.namespace == 'contact', Code.IDENTITY)
    return [{'corpId': identity.tenant_id, 'userId': identity.user_id}]


def _select(value):
    return {'id': f'SYNTHETIC-opt-{value}', 'name': value}


def encode_loan(loan: Loan, fields) -> dict:
    cells = {
        fields.state: _select(loan.state.value),
        fields.quantity: str(loan.quantity),
        fields.tracked: _select('true' if loan.tracked else 'false'),
        fields.physical_ids: json.dumps(list(loan.physical_ids), ensure_ascii=True),
        fields.due_at: loan.due_at.isoformat(),
        fields.config_version: loan.config_version,
        fields.borrower: _put_identity(loan.borrower),
        fields.approver: _put_identity(loan.approver),
        fields.manager: _put_identity(loan.manager),
        fields.item_container: loan.item.container_id,
        fields.item_id: loan.item.resource_id,
        fields.consumed_events: json.dumps(list(loan.consumed_events), ensure_ascii=True),
        fields.application_evidence: loan.application_evidence,
    }
    if loan.return_ref is not None:
        cells[fields.return_container] = loan.return_ref.container_id
        cells[fields.return_id] = loan.return_ref.resource_id
    return cells


def encode_inventory(stock: Inventory, fields) -> dict:
    return {
        fields.available: str(stock.available),
        fields.reserved: str(stock.reserved),
        fields.borrowed: str(stock.borrowed),
        fields.available_ids: json.dumps(list(stock.available_ids), ensure_ascii=True),
        fields.reserved_ids: json.dumps(list(stock.reserved_ids), ensure_ascii=True),
        fields.borrowed_ids: json.dumps(list(stock.borrowed_ids), ensure_ascii=True),
        fields.revision: stock.revision,
    }


def decode_loan(ref: Resource, cells, fields) -> Loan:
    try:
        tracked = read_single_select(cells, fields.tracked).name == 'true'
        return_ref = None
        return_id = read_text_or_empty(cells, fields.return_id)
        if return_id:
            return_ref = Resource('record', ref.tenant_id,
                                  read_text(cells, fields.return_container), return_id)
        item = Resource('record', ref.tenant_id,
                        read_text(cells, fields.item_container),
                        read_text(cells, fields.item_id))
        return Loan(
            ref, item,
            _identity(cells, fields.borrower, ref.tenant_id),
            _identity(cells, fields.approver, ref.tenant_id),
            _identity(cells, fields.manager, ref.tenant_id),
            _int_count(cells, fields.quantity, zero=False),
            tracked,
            _text_list(cells, fields.physical_ids),
            read_datetime(cells, fields.due_at),
            read_text(cells, fields.config_version),
            State(read_single_select(cells, fields.state).name),
            return_ref,
            _text_list(cells, fields.consumed_events),
            read_text(cells, fields.application_evidence),
        )
    except ContractError:
        raise
    except (DingTalkShapeError, ValueError) as exc:
        _shape(exc)


def decode_inventory(ref: Resource, cells, fields) -> Inventory:
    try:
        return Inventory(
            ref,
            _int_count(cells, fields.available),
            _int_count(cells, fields.reserved),
            _int_count(cells, fields.borrowed),
            _text_list(cells, fields.available_ids),
            _text_list(cells, fields.reserved_ids),
            _text_list(cells, fields.borrowed_ids),
            read_text(cells, fields.revision),
        )
    except ContractError:
        raise
    except (DingTalkShapeError, ValueError) as exc:
        _shape(exc)
