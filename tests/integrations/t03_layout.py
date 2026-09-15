"""Test-only helper: copy ledger FieldMap keys into an EntryFieldMap.

Production binding.json carries two maps. Tests that still share one synthetic
table use this copy; it is not a production fallback.
"""
from integrations.dingtalk.layout import EntryFieldMap


def entry_fields_from(fields):
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
