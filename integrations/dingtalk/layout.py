"""Adapter-owned field identifiers. Not public contract names."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldMap:
    state: str
    quantity: str
    tracked: str
    physical_ids: str
    due_at: str
    config_version: str
    borrower: str
    approver: str
    manager: str
    item_container: str
    item_id: str
    return_container: str
    return_id: str
    consumed_events: str
    application_evidence: str
    available: str
    reserved: str
    borrowed: str
    available_ids: str
    reserved_ids: str
    borrowed_ids: str
    revision: str
    decision: str
    loan_container: str
    loan_id: str
    occurred_at: str
    action: str
    operation_id: str


@dataclass(frozen=True)
class EntryFieldMap:
    """Stage-entry (form) field IDs. Distinct from ledger FieldMap for the overlapping keys."""
    quantity: str
    physical_ids: str
    config_version: str
    borrower: str
    approver: str
    manager: str
    return_container: str
    return_id: str
    decision: str
    loan_container: str
    loan_id: str
    occurred_at: str
    action: str
    operation_id: str


SYNTHETIC_FIELDS = FieldMap(
    state='fldSYN-state',
    quantity='fldSYN-qty',
    tracked='fldSYN-tracked',
    physical_ids='fldSYN-pids',
    due_at='fldSYN-due',
    config_version='fldSYN-cfg',
    borrower='fldSYN-borrower',
    approver='fldSYN-approver',
    manager='fldSYN-manager',
    item_container='fldSYN-item-container',
    item_id='fldSYN-item-id',
    return_container='fldSYN-return-container',
    return_id='fldSYN-return-id',
    consumed_events='fldSYN-events',
    application_evidence='fldSYN-app-ev',
    available='fldSYN-avail',
    reserved='fldSYN-rsv',
    borrowed='fldSYN-brw',
    available_ids='fldSYN-avail-ids',
    reserved_ids='fldSYN-rsv-ids',
    borrowed_ids='fldSYN-brw-ids',
    revision='fldSYN-rev',
    decision='fldSYN-decision',
    loan_container='fldSYN-loan-container',
    loan_id='fldSYN-loan-id',
    occurred_at='fldSYN-occurred',
    action='fldSYN-action',
    operation_id='fldSYN-opid',
)


SYNTHETIC_ENTRY_FIELDS = EntryFieldMap(
    quantity='fldSYN-entry-qty',
    physical_ids='fldSYN-entry-pids',
    config_version='fldSYN-entry-cfg',
    borrower='fldSYN-entry-borrower',
    approver='fldSYN-entry-approver',
    manager='fldSYN-entry-manager',
    return_container='fldSYN-entry-return-container',
    return_id='fldSYN-entry-return-id',
    decision='fldSYN-decision',
    loan_container='fldSYN-loan-container',
    loan_id='fldSYN-loan-id',
    occurred_at='fldSYN-occurred',
    action='fldSYN-action',
    operation_id='fldSYN-opid',
)


def entry_fields_from(fields):
    """Copy entry-used keys from a ledger FieldMap. Tests that share one table only."""
    return EntryFieldMap(
        quantity=fields.quantity,
        physical_ids=fields.physical_ids,
        config_version=fields.config_version,
        borrower=fields.borrower,
        approver=fields.approver,
        manager=fields.manager,
        return_container=fields.return_container,
        return_id=fields.return_id,
        decision=fields.decision,
        loan_container=fields.loan_container,
        loan_id=fields.loan_id,
        occurred_at=fields.occurred_at,
        action=fields.action,
        operation_id=fields.operation_id,
    )
