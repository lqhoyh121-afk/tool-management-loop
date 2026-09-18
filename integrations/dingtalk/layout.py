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


@dataclass(frozen=True)
class ApplicationFieldMap:
    """Application collection table field IDs. Distinct from stage-entry EntryFieldMap.

    ``item_container`` / ``item_id`` / ``due_at`` are what turns an application row
    into a ledger row (issue #78). They are optional only so an existing binding
    keeps loading: a missing or ``unset:``-prefixed value means "this instance has
    not declared that question", and the discovery then fails closed and reports
    the row as unregistered instead of inventing an item or a due time.
    """

    quantity: str
    physical_ids: str
    borrower: str
    occurred_at: str
    item_container: str = ''
    item_id: str = ''
    due_at: str = ''


@dataclass(frozen=True)
class ReturnFormFieldMap:
    """Return form view on the stage-entry table: borrower and return time.

    ``item`` is the optional「归还物品」question (#77). Bound *and* filled on the
    row, it narrows matching from "this borrower's only open loan" to "this
    borrower's only open loan of that item". Unbound, or bound but empty on the
    row, keeps the pre-#77 borrower-only match. Name it after the field the
    operator adds on the live form view; there is no default field id.
    """
    borrower: str
    occurred_at: str
    item: str = ''


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

SYNTHETIC_APPLY_FIELDS = ApplicationFieldMap(
    quantity='fldSYN-apply-qty',
    physical_ids='fldSYN-apply-pids',
    borrower='fldSYN-apply-borrower',
    occurred_at='fldSYN-apply-occurred',
    item_container='fldSYN-apply-item-container',
    item_id='fldSYN-apply-item-id',
    due_at='fldSYN-apply-due',
)

SYNTHETIC_RETURN_FORM_FIELDS = ReturnFormFieldMap(
    borrower='fldSYN-return-borrower',
    occurred_at='fldSYN-return-occurred',
    item='fldSYN-return-item',
)
