"""Durable OperationStore. Recovery snapshots only; DingTalk remains the ledger."""
import json
from pathlib import Path

from contracts.model import Code, Outcome, require
from contracts.ports import StageRequest

from .snapshot import decode_entry, encode_entry


def _intent_refs(intent):
    if isinstance(intent, StageRequest):
        return intent.loan.ref, None
    return intent.before.ref, intent.before.item


def is_resolved(receipt):
    """这条本地流水是否已经有终态结论（verified / not_applied / not_sent）。

    回查口径的唯一来源：``unresolved_ids`` 用它筛未决，驱动侧也用它判断一次回查到底
    有没有结清 —— 两处必须同一口径，否则同一张单会同时被算成「已回查」和「挂起」。
    """
    return receipt is not None and receipt.outcome in (
        Outcome.VERIFIED, Outcome.NOT_APPLIED, Outcome.NOT_SENT)


class FileJournal:
    """One JSON file per operation_id under a runtime operations directory."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, intent):
        entries = self._entries()
        if intent.operation_id in entries:
            require(entries[intent.operation_id][0] == intent, Code.OP_CONFLICT)
            return
        new_loan_ref, new_item_ref = _intent_refs(intent)
        for old, receipt in entries.values():
            old_loan_ref, old_item_ref = _intent_refs(old)
            shares_target = (old_loan_ref == new_loan_ref
                             or (new_item_ref is not None and old_item_ref == new_item_ref))
            if shares_target:
                require(is_resolved(receipt), Code.UNKNOWN)
        self._write(intent.operation_id, intent, None)

    def load(self, operation_id):
        path = self._path(operation_id)
        if not path.exists():
            raise KeyError(operation_id)
        return decode_entry(json.loads(path.read_text(encoding='utf-8')))

    def save_receipt(self, receipt):
        intent, _ = self.load(receipt.operation_id)
        self._write(receipt.operation_id, intent, receipt)

    def ids(self):
        return tuple(sorted(path.stem for path in self.root.glob('*.json')))

    def unresolved_ids(self):
        pending = []
        for operation_id in self.ids():
            _, receipt = self.load(operation_id)
            if not is_resolved(receipt):
                pending.append(operation_id)
        return tuple(pending)

    def _entries(self):
        return {operation_id: self.load(operation_id) for operation_id in self.ids()}

    def _path(self, operation_id):
        require(isinstance(operation_id, str) and operation_id.strip(), Code.INVALID)
        return self.root / f'{operation_id}.json'

    def _write(self, operation_id, intent, receipt):
        path = self._path(operation_id)
        tmp = path.with_suffix('.tmp')
        payload = json.dumps(encode_entry(intent, receipt), ensure_ascii=True, indent=2)
        tmp.write_text(payload + '\n', encoding='utf-8')
        tmp.replace(path)
