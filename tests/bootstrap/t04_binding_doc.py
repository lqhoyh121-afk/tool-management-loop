"""Synthetic binding document for T04 tests. Not a generic support module."""

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
        'container_id': 'synthetic-forms',
        'resource_id': 'synthetic-apply-entry',
    },
}


def binding_document(**overrides):
    data = {key: value for key, value in DOC.items()}
    data.update(overrides)
    return data


def ledger_html():
    return (
        '<table><tr><td>available</td><td>reserved</td><td>borrowed</td>'
        '<td>revision</td></tr><tr><td>5</td><td>0</td><td>0</td>'
        '<td>synthetic-rev-1</td></tr></table>'
    )
