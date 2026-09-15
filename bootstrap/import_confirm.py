"""Confirm a ledger workbook after an independent re-read. No DingTalk writes."""
from hashlib import sha256
from pathlib import Path
import json

from contracts.model import Code, ContractError, require

from .binding import require_complete
from .file_preview import PreviewError, preview_workbook
from .gate import assert_business_allowed

RECEIPT_NAME = 'import-receipt.json'
REQUIRED_HEADERS = ('available', 'reserved', 'borrowed', 'revision')


def receipt_path(runtime):
    return Path(runtime) / RECEIPT_NAME


def file_digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _headers(preview):
    require(bool(preview.sheets), Code.EVIDENCE)
    return tuple(preview.sheets[0].headers)


def _fingerprint(preview):
    sheets = tuple(
        (sheet.name, tuple(sheet.headers), tuple(tuple(row) for row in sheet.rows))
        for sheet in preview.sheets
    )
    return (preview.kind, sheets)


def confirm_import(path, runtime, binding, lease, locks):
    require_complete(binding)
    assert_business_allowed(binding, lease, locks)
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    stored = receipt_path(runtime)
    if stored.exists():
        raise ContractError(Code.CONFIG)
    try:
        first = preview_workbook(path)
        second = preview_workbook(path)
    except PreviewError as exc:
        raise ContractError(Code.EVIDENCE) from exc
    require(_fingerprint(first) == _fingerprint(second), Code.READBACK)
    headers = _headers(first)
    missing = [name for name in REQUIRED_HEADERS if name not in headers]
    if missing:
        raise ContractError(Code.EVIDENCE)
    receipt = {
        'source_name': first.source_name,
        'digest': file_digest(path),
        'kind': first.kind,
        'headers': list(headers),
        'config_version': binding.config_version,
        'ledger': {
            'tenant_id': binding.ledger.tenant_id,
            'container_key': binding.ledger.container_key,
        },
    }
    stored.write_text(json.dumps(receipt, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    return receipt
