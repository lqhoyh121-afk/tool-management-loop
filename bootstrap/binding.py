"""Persist a confirmed RuntimeBinding. Never overwrite, never treat ready as binding."""
import json
from pathlib import Path

from contracts.model import Code, ContractError, Identity, Resource, require, text
from contracts.ports import LedgerScope, RuntimeBinding
from integrations.dingtalk.layout import EntryFieldMap, FieldMap

BINDING_NAME = 'binding.json'
TMP_NAME = 'binding.json.tmp'
READY_NAME = 'ready.json'


def binding_path(runtime):
    return Path(runtime) / BINDING_NAME


def interrupted_path(runtime):
    return Path(runtime) / TMP_NAME


def ready_path(runtime):
    return Path(runtime) / READY_NAME


def _identity(data, code=Code.IDENTITY):
    require(isinstance(data, dict), code)
    identity = Identity(str(data.get('namespace', '')), str(data.get('tenant_id', '')),
                        str(data.get('user_id', '')))
    require(identity.namespace == 'contact', code)
    return identity


def _resource(data):
    require(isinstance(data, dict), Code.EVIDENCE)
    return Resource(str(data.get('kind', '')), str(data.get('tenant_id', '')),
                    str(data.get('container_id', '')), str(data.get('resource_id', '')))


def binding_from_document(data):
    require(isinstance(data, dict), Code.INVALID)
    ledger_raw = data.get('ledger')
    require(isinstance(ledger_raw, dict), Code.WRONG_LOAN)
    ledger = LedgerScope(str(ledger_raw.get('tenant_id', '')),
                         str(ledger_raw.get('container_key', '')))
    flags = (
        data.get('ordinary_ledger_denied') is True,
        data.get('restricted_forms_verified') is True,
        data.get('account_read_write_verified') is True,
        data.get('explicitly_confirmed') is True,
    )
    binding = RuntimeBinding(
        _identity(data.get('account')),
        ledger,
        str(data.get('config_version', '')),
        _identity(data.get('approver')),
        _identity(data.get('manager')),
        str(data.get('evidence_ref', '')),
        *flags,
    )
    require_complete(binding)
    fields, entry_fields = field_maps_from_document(data)
    entry = data.get('application_entry')
    require(entry is not None, Code.EVIDENCE)
    application = _resource(entry)
    require(application.kind in ('form', 'record'), Code.EVIDENCE)
    require(application.tenant_id == binding.account.tenant_id, Code.IDENTITY)
    return binding, application, fields, entry_fields


def require_complete(binding):
    require(binding is not None, Code.CONFIG)
    text(binding.config_version)
    text(binding.evidence_ref)
    require(binding.account.namespace == 'contact', Code.IDENTITY)
    require(all(item is True for item in (
        binding.ordinary_ledger_denied,
        binding.restricted_forms_verified,
        binding.account_read_write_verified,
        binding.explicitly_confirmed,
    )), Code.EVIDENCE)
    return binding


def field_maps_from_document(data):
    """Ledger FieldMap and stage-entry EntryFieldMap are both required. No fallback."""
    require(isinstance(data, dict), Code.INVALID)
    raw_fields = data.get('fields')
    raw_entry = data.get('entry_fields')
    require(isinstance(raw_fields, dict), Code.CONFIG)
    require(isinstance(raw_entry, dict), Code.CONFIG)
    try:
        fields = FieldMap(**raw_fields)
        entry_fields = EntryFieldMap(**raw_entry)
    except TypeError as exc:
        raise ContractError(Code.CONFIG) from exc
    return fields, entry_fields


def load_binding(runtime):
    data = read_binding_document(runtime)
    if data is None:
        return None, None
    binding, application, _, _ = binding_from_document(data)
    return binding, application


def save_binding(runtime, document):
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    path = binding_path(runtime)
    tmp = interrupted_path(runtime)
    if tmp.exists() and not path.exists():
        raise ContractError(Code.EVIDENCE)
    if path.exists():
        raise ContractError(Code.CONFIG)
    binding, application, _, _ = binding_from_document(document)
    payload = json.dumps(document, ensure_ascii=True, indent=2) + '\n'
    tmp.write_text(payload, encoding='utf-8')
    tmp.replace(path)
    return binding, application


def read_binding_document(runtime):
    runtime = Path(runtime)
    path = binding_path(runtime)
    tmp = interrupted_path(runtime)
    if tmp.exists() and not path.exists():
        raise ContractError(Code.EVIDENCE)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def document_from_files(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    require(isinstance(data, dict), Code.INVALID)
    return data
