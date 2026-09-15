import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t04_binding_doc import binding_document

from bootstrap.binding import save_binding
from bootstrap.wizard import main


class WizardTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_check_env_exit_nonzero_without_binding(self):
        stdout = io.StringIO()
        code = main(
            ['--check-env'],
            stdin=io.StringIO(''),
            stdout=stdout,
            wait_on_error=False,
            environ_kwargs={'system_name': 'Windows', 'version_info': (3, 11, 4, 'final', 0), 'tkinter_available': True},
        )
        self.assertEqual(code, 1)
        text = stdout.getvalue()
        self.assertIn('Python 运行时: 通过', text)
        self.assertIn('钉钉授权: 未通过', text)
        self.assertNotIn('钉钉授权: 待确认', text)

    def test_check_env_exit_zero_when_binding_complete(self):
        save_binding(self.root, binding_document())
        stdout = io.StringIO()
        code = main(
            ['--check-env', '--runtime', str(self.root)],
            stdin=io.StringIO(''),
            stdout=stdout,
            wait_on_error=False,
            environ_kwargs={'system_name': 'Windows', 'version_info': (3, 11, 4, 'final', 0),
                            'tkinter_available': True, 'runtime_dir': str(self.root)},
        )
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn('人员与路由绑定: 通过', text)
        self.assertIn('钉钉授权: 通过', text)
        self.assertIn('台账字段映射与导入: 未通过', text)

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
        self.assertIn('环境或绑定未通过', stdout.getvalue())
        self.assertNotIn('should-not-run', stdout.getvalue())

    def test_require_ready_blocks_preview_flag(self):
        path = self.root / 'demo.xls'
        path.write_text('<table><tr><td>列甲</td></tr><tr><td>值乙</td></tr></table>', encoding='utf-8')
        stdout = io.StringIO()
        code = main(
            ['--require-ready', '--preview', str(path)],
            stdin=io.StringIO(''),
            stdout=stdout,
            wait_on_error=False,
            environ_kwargs={'system_name': 'Linux', 'version_info': (3, 9, 0, 'final', 0), 'tkinter_available': False},
        )
        self.assertEqual(code, 1)
        text = stdout.getvalue()
        self.assertIn('环境或绑定未通过', text)
        self.assertNotIn('值乙', text)
        self.assertNotIn('业务就绪', text.split('环境或绑定未通过', 1)[0])

    def test_require_ready_rejects_ready_file_without_binding(self):
        (self.root / 'ready.json').write_text(json.dumps({'ready': True}), encoding='utf-8')
        stdout = io.StringIO()
        code = main(
            ['--require-ready', '--check-env', '--runtime', str(self.root)],
            stdin=io.StringIO(''),
            stdout=stdout,
            wait_on_error=False,
            environ_kwargs={'system_name': 'Windows', 'version_info': (3, 11, 0, 'final', 0),
                            'tkinter_available': True, 'runtime_dir': str(self.root)},
        )
        self.assertEqual(code, 1)
        text = stdout.getvalue()
        self.assertIn('钉钉授权: 未通过', text)
        self.assertIn('ready 标记不能绕过', text)
        self.assertNotIn('钉钉授权: 通过', text)

    def test_bind_refuses_overwrite(self):
        source = self.root / 'bind.json'
        source.write_text(json.dumps(binding_document()), encoding='utf-8')
        kwargs = {'system_name': 'Windows', 'version_info': (3, 11, 0, 'final', 0),
                  'tkinter_available': True, 'runtime_dir': str(self.root)}
        first = main(
            ['--runtime', str(self.root), '--bind', str(source)],
            stdin=io.StringIO(''), stdout=io.StringIO(), wait_on_error=False,
            environ_kwargs=kwargs,
        )
        self.assertEqual(first, 0)
        stdout = io.StringIO()
        second = main(
            ['--runtime', str(self.root), '--bind', str(source)],
            stdin=io.StringIO(''), stdout=stdout, wait_on_error=False,
            environ_kwargs=kwargs,
        )
        self.assertEqual(second, 1)
        self.assertIn('闸门拒绝', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
