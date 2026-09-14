import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.wizard import main


class WizardTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_check_env_exit_zero_when_injected_pass(self):
        stdout = io.StringIO()
        code = main(
            ['--check-env'],
            stdin=io.StringIO(''),
            stdout=stdout,
            wait_on_error=False,
            environ_kwargs={'system_name': 'Windows', 'version_info': (3, 11, 4, 'final', 0), 'tkinter_available': True},
        )
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn('Python 运行时: 通过', text)
        self.assertIn('钉钉授权: 待确认', text)

    def test_preview_html_file(self):
        path = self.root / 'demo.xls'
        path.write_text('<table><tr><td>列甲</td></tr><tr><td>值乙</td></tr></table>', encoding='utf-8')
        stdout = io.StringIO()
        code = main(['--preview', str(path)], stdin=io.StringIO(''), stdout=stdout, wait_on_error=False)
        self.assertEqual(code, 0)
        self.assertIn('值乙', stdout.getvalue())

    def test_preview_failure_is_visible(self):
        stdout = io.StringIO()
        code = main(['--preview', str(self.root / 'nope.xls')], stdin=io.StringIO(''), stdout=stdout, wait_on_error=False)
        self.assertEqual(code, 1)
        self.assertIn('预览失败', stdout.getvalue())

    def test_cancel_file_selection(self):
        stdout = io.StringIO()
        stdin = io.StringIO('1\n2\n')
        code = main(
            [],
            stdin=stdin,
            stdout=stdout,
            picker=lambda: None,
            wait_on_error=False,
            environ_kwargs={'system_name': 'Windows', 'version_info': (3, 12, 0, 'final', 0), 'tkinter_available': True},
        )
        self.assertEqual(code, 0)
        self.assertIn('已取消文件选择', stdout.getvalue())

    def test_menu_then_preview_then_exit(self):
        path = self.root / 'picked.xls'
        path.write_text('<table><tr><td>H</td></tr><tr><td>R</td></tr></table>', encoding='utf-8')
        stdout = io.StringIO()
        code = main(
            [],
            stdin=io.StringIO('1\n2\n'),
            stdout=stdout,
            picker=lambda: str(path),
            wait_on_error=False,
            environ_kwargs={'system_name': 'Windows', 'version_info': (3, 12, 0, 'final', 0), 'tkinter_available': True},
        )
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn('原始表头', text)
        self.assertIn('已退出', text)

    def test_unknown_option_does_not_crash(self):
        stdout = io.StringIO()
        code = main(
            [],
            stdin=io.StringIO('9\n2\n'),
            stdout=stdout,
            picker=lambda: None,
            wait_on_error=False,
            environ_kwargs={'system_name': 'Windows', 'version_info': (3, 12, 0, 'final', 0), 'tkinter_available': True},
        )
        self.assertEqual(code, 0)
        self.assertIn('无法识别的选项', stdout.getvalue())

    def test_require_ready_blocks_on_failure(self):
        stdout = io.StringIO()
        code = main(
            ['--require-ready'],
            stdin=io.StringIO('1\n'),
            stdout=stdout,
            picker=lambda: 'should-not-run',
            wait_on_error=False,
            environ_kwargs={'system_name': 'Linux', 'version_info': (3, 9, 0, 'final', 0), 'tkinter_available': False},
        )
        self.assertEqual(code, 1)
        self.assertIn('环境未通过', stdout.getvalue())
        self.assertNotIn('should-not-run', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
