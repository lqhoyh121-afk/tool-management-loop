"""SYNTHETIC ONLY: in-memory adapter/journal/lease for contract consumers.

No production locking, persistence, authority or external platform is implemented.
"""
from dataclasses import replace

from contracts.flow import Receipt, verify
from contracts.model import Code, Outcome, require
from contracts.ports import check_binding


class SyntheticLease:
    def __init__(self):
        self.held = None

    def acquire(self, ledger, account):
        require(self.held is None, Code.INSTANCE)
        self.held = "synthetic-lease"
        return self.held

    def assert_held(self, lease):
        require(self.held is not None and lease == self.held, Code.INSTANCE)

    def release(self, lease):
        self.assert_held(lease)
        self.held = None


class SyntheticJournal:
    def __init__(self):
        self.entries = {}

    def prepare(self, intent):
        if intent.operation_id in self.entries:
            require(self.entries[intent.operation_id][0] == intent, Code.OP_CONFLICT)
            return
        for old, receipt in self.entries.values():
            if (old.before.ref == intent.before.ref or old.stock_before.ref == intent.stock_before.ref):
                require(receipt is not None and receipt.outcome in
                        (Outcome.VERIFIED, Outcome.NOT_APPLIED, Outcome.NOT_SENT), Code.UNKNOWN)
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
