import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t04_binding_doc import binding_document

from bootstrap.binding import save_binding
from bootstrap.gate import assert_business_allowed
from bootstrap.instance import MachineLock
from contracts.model import Code, ContractError


class GateTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.runtime = self.root / 'runtime'
        self.locks = MachineLock(self.root / 'locks')
        self.binding, _entry = save_binding(self.runtime, binding_document())
        (self.runtime / 'ready.json').write_text(
            json.dumps({'ready': True}), encoding='utf-8')

    def tearDown(self):
        self._temp.cleanup()

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_bypass_bat_still_requires_lease_and_binding(self):
        self.blocked(Code.INSTANCE, lambda: assert_business_allowed(
            self.binding, 'missing-lease', self.locks))
        lease = self.locks.acquire(self.binding.ledger, self.binding.account)
        assert_business_allowed(self.binding, lease, self.locks)
        self.locks.release(lease)

    def test_ready_file_does_not_replace_binding(self):
        self.blocked(Code.CONFIG, lambda: assert_business_allowed(None, 'x', self.locks))
        self.assertTrue((self.runtime / 'ready.json').exists())
