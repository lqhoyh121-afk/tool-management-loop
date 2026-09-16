"""Synthetic binding document for T04 tests. Not a generic support module."""
from copy import deepcopy
from dataclasses import asdict

from integrations.dingtalk.layout import (
    SYNTHETIC_APPLY_FIELDS, SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_FIELDS,
    SYNTHETIC_RETURN_FORM_FIELDS)

DOC = {
    'account': {'namespace': 'contact', 'tenant_id': 'synthetic-org',
                'user_id': 'synthetic-manager'},
    'ledger': {'tenant_id': 'synthetic-org', 'container_key': 'synthetic-stock'},
    'config_version': 'synthetic-config-v1',
    'approver': {'namespace': 'contact', 'tenant_id': 'synthetic-org',
                 'user_id': 'synthetic-manager'},
    'manager': {'namespace': 'contact', 'tenant_id': 'synthetic-org',
                'user_id': 'synthetic-manager'},
    'evidence_ref': 'synthetic-binding-readback',
    'ordinary_ledger_denied': True,
    'restricted_forms_verified': True,
    'account_read_write_verified': True,
    'explicitly_confirmed': True,
    'application_entry': {
        'kind': 'form',
        'tenant_id': 'synthetic-org',
        'container_id': 'synthetic-apply-forms',
        'resource_id': 'synthetic-apply-entry',
    },
    'fields': asdict(SYNTHETIC_FIELDS),
    'entry_fields': asdict(SYNTHETIC_ENTRY_FIELDS),
    'apply_fields': asdict(SYNTHETIC_APPLY_FIELDS),
    'return_form_fields': asdict(SYNTHETIC_RETURN_FORM_FIELDS),
    'loan_container': 'synthetic-loans',
    'form_container': 'synthetic-forms',
    'todo_container': 'synthetic-todos',
}


def binding_document(**overrides):
    data = deepcopy(DOC)
    data.update(overrides)
    return data


def ledger_html():
    return (
        '<table><tr><td>available</td><td>reserved</td><td>borrowed</td>'
        '<td>revision</td></tr><tr><td>5</td><td>0</td><td>0</td>'
        '<td>synthetic-rev-1</td></tr></table>'
    )
