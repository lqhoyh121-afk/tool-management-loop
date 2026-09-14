"""SYNTHETIC ONLY: in-memory adapter/journal/lease for contract consumers.

No production locking, persistence, authority or external platform is implemented.
"""
from dataclasses import replace

from contracts.flow import Receipt, verify
from contracts.model import Code, Outcome, require
from contracts.ports import StageRequest, check_binding, lease_key


class SyntheticLease:
    def __init__(self):
        self.held = {}

    def acquire(self, scope, account):
        key = lease_key(scope)
        require(key not in self.held, Code.INSTANCE)
        self.held[key] = "synthetic-lease-" + key
        return self.held[key]

    def assert_held(self, lease):
        require(lease in self.held.values(), Code.INSTANCE)

    def release(self, lease):
        self.assert_held(lease)
        self.held = {key: value for key, value in self.held.items() if value != lease}


def _intent_refs(intent):
    """Both WriteIntent and StageRequest: (loan ref, item ref or None)."""
    if isinstance(intent, StageRequest):
        return intent.loan.ref, None
    return intent.before.ref, intent.before.item


def _resolved(receipt):
    return receipt is not None and receipt.outcome in (
        Outcome.VERIFIED, Outcome.NOT_APPLIED, Outcome.NOT_SENT)


class SyntheticJournal:
    def __init__(self):
        self.entries = {}

    def prepare(self, intent):
        if intent.operation_id in self.entries:
            require(self.entries[intent.operation_id][0] == intent, Code.OP_CONFLICT)
            return
        new_loan_ref, new_item_ref = _intent_refs(intent)
        for old, receipt in self.entries.values():
            old_loan_ref, old_item_ref = _intent_refs(old)
            shares_target = (old_loan_ref == new_loan_ref
                             or (new_item_ref is not None and old_item_ref == new_item_ref))
            if shares_target:
                require(_resolved(receipt), Code.UNKNOWN)
        self.entries[intent.operation_id] = (intent, None)

    def load(self, operation_id):
        return self.entries[operation_id]

    def save_receipt(self, receipt):
        intent, _ = self.load(receipt.operation_id)
        self.entries[receipt.operation_id] = (intent, receipt)


class SyntheticWriter:
    def __init__(self, current, inventory, journal, leases):
        self.current = current
        self.inventory = inventory
        self.journal = journal
        self.leases = leases
        self.writes = 0
        self.queries = 0
        self.lose_response = False

    def submit(self, intent, binding, lease):
        self.leases.assert_held(lease)
        check_binding(binding, intent.before)
        self.journal.prepare(intent)
        _, receipt = self.journal.load(intent.operation_id)
        if receipt is not None:
            if receipt.outcome == Outcome.UNKNOWN:
                require(False, Code.UNKNOWN)
            if receipt.outcome == Outcome.VERIFIED:
                return receipt
        require(self.current == intent.before and self.inventory == intent.stock_before, Code.CONFLICT)
        # Crash-after-send-start must be unknown, never not_sent.
        self.journal.save_receipt(Receipt(intent.operation_id, Outcome.UNKNOWN))
        self.current = intent.after
        self.inventory = replace(intent.stock_after, revision="synthetic-next-revision")
        self.writes += 1
        if self.lose_response:
            return Receipt(intent.operation_id, Outcome.UNKNOWN)
        return self.query(intent)

    def query(self, intent):
        self.queries += 1
        receipt = Receipt(intent.operation_id, Outcome.VERIFIED, "synthetic-independent-read",
                          self.current, self.inventory)
        if verify(intent, receipt).outcome != Outcome.VERIFIED:
            receipt = Receipt(intent.operation_id, Outcome.UNKNOWN)
        self.journal.save_receipt(receipt)
        return receipt
