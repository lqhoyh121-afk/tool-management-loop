import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t04_binding_doc import binding_document, ledger_html

from bootstrap.binding import save_binding
from bootstrap.import_confirm import confirm_import, receipt_path
from bootstrap.instance import MachineLock
from bootstrap.wizard import main
from contracts.model import Code, ContractError


class ImportConfirmTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.runtime = self.root / 'runtime'
        self.locks = MachineLock(self.root / 'locks')
        self.binding, _entry = save_binding(self.runtime, binding_document())
        self.workbook = self.root / '台账 合成.xls'
        self.workbook.write_text(ledger_html(), encoding='utf-8')

    def tearDown(self):
        self._temp.cleanup()

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_confirm_readback_and_refuse_repeat(self):
        lease = self.locks.acquire(self.binding.ledger, self.binding.account)
        receipt = confirm_import(self.workbook, self.runtime, self.binding, lease, self.locks)
        self.assertEqual(receipt['source_name'], '台账 合成.xls')
        self.assertNotIn('\\', receipt['source_name'])
        self.assertNotIn('/', receipt['source_name'])
        self.blocked(Code.CONFIG, lambda: confirm_import(
            self.workbook, self.runtime, self.binding, lease, self.locks))
        self.locks.release(lease)

    def test_missing_required_headers_are_not_invented(self):
        bad = self.root / 'bad.xls'
        bad.write_text('<table><tr><td>ColA</td></tr><tr><td>1</td></tr></table>', encoding='utf-8')
        lease = self.locks.acquire(self.binding.ledger, self.binding.account)
        self.blocked(Code.EVIDENCE, lambda: confirm_import(
            bad, self.runtime, self.binding, lease, self.locks))
        self.assertFalse(receipt_path(self.runtime).exists())
        self.locks.release(lease)

    def test_wizard_confirm_import_uses_gate(self):
        stdout = io.StringIO()
        code = main(
            ['--runtime', str(self.runtime), '--lock-root', str(self.root / 'locks'),
             '--confirm-import', str(self.workbook)],
            stdin=io.StringIO(''),
            stdout=stdout,
            wait_on_error=False,
            environ_kwargs={'system_name': 'Windows', 'version_info': (3, 11, 0, 'final', 0),
                            'tkinter_available': True, 'runtime_dir': str(self.runtime)},
        )
        self.assertEqual(code, 0)
        self.assertIn('导入已确认并回读', stdout.getvalue())
        self.assertTrue(receipt_path(self.runtime).exists())
