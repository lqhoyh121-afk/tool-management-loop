import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t04_binding_doc import binding_document

from bootstrap.binding import save_binding
from bootstrap.instance import MachineLock
from contracts.model import Code, ContractError, Identity, Resource
from contracts.ports import LedgerScope, lease_key


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.locks = MachineLock(self.root / 'locks')
        self.binding, _entry = save_binding(self.root / 'runtime', binding_document())
        self.account = self.binding.account
        self.scope = self.binding.ledger

    def tearDown(self):
        self._temp.cleanup()

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_second_instance_blocked_even_with_other_runtime_and_account(self):
        lease = self.locks.acquire(self.scope, self.account)
        other = MachineLock(self.root / 'locks')
        other_account = Identity('contact', 'synthetic-org', 'synthetic-borrower')
        self.blocked(Code.INSTANCE, lambda: other.acquire(self.scope, other_account))
        self.locks.assert_held(lease)
        self.locks.release(lease)
        recovered = other.acquire(self.scope, other_account)
        other.release(recovered)

    def test_lock_key_ignores_item_and_runtime_directory(self):
        self.assertEqual(lease_key(self.scope), 'synthetic-org::synthetic-stock')
        item = Resource('record', 'synthetic-org', 'synthetic-stock', 'synthetic-item')
        self.assertEqual(lease_key(LedgerScope.from_record(item)), 'synthetic-org::synthetic-stock')

    def test_different_scopes_are_independent(self):
        first = self.locks.acquire(self.scope, self.account)
        other_scope = LedgerScope('synthetic-org', 'synthetic-other-stock')
        second = self.locks.acquire(other_scope, self.account)
        self.locks.release(first)
        self.locks.release(second)

    def test_lost_lease_fails_closed(self):
        lease = self.locks.acquire(self.scope, self.account)
        self.locks.release(lease)
        self.blocked(Code.INSTANCE, lambda: self.locks.assert_held(lease))
