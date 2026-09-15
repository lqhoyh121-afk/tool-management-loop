import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t04_binding_doc import binding_document

from bootstrap.binding import save_binding
from bootstrap.env_check import check_environment, format_report, has_blocking_failure, has_gate_failure
from bootstrap import env_check


class EnvCheckTests(unittest.TestCase):
    def test_pass_windows_python(self):
        items = {item.key: item for item in check_environment(
            system_name='Windows',
            version_info=(3, 11, 0, 'final', 0),
            tkinter_available=True,
        )}
        self.assertEqual(items['os'].status, 'pass')
        self.assertEqual(items['python'].status, 'pass')
        self.assertEqual(items['file_dialog'].status, 'pass')
        self.assertEqual(items['people_routing'].status, 'fail')
        self.assertTrue(has_gate_failure(items.values()))

    def test_fail_old_python_and_non_windows(self):
        items = {item.key: item for item in check_environment(
            system_name='Linux',
            version_info=(3, 10, 9, 'final', 0),
            tkinter_available=False,
        )}
        self.assertEqual(items['os'].status, 'fail')
        self.assertEqual(items['python'].status, 'fail')
        self.assertEqual(items['file_dialog'].status, 'fail')
        self.assertTrue(has_blocking_failure(items.values()))

    def test_missing_binding_is_fail_not_pending(self):
        items = check_environment(system_name='Windows', version_info=(3, 12, 0, 'final', 0),
                                  tkinter_available=True)
        pending = [item for item in items if item.status == 'pending']
        self.assertEqual(pending, [])
        report = format_report(items)
        self.assertIn('人员与路由绑定: 未通过', report)
        self.assertIn('ready 标记不能当作已绑定', report)
        self.assertNotIn('钉钉授权: 通过', report)
        self.assertNotIn('待确认（等待 T02', report)

    def test_complete_binding_passes_gate_without_ready_file(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            save_binding(runtime, binding_document())
            (runtime / 'ready.json').write_text(json.dumps({'ready': True}), encoding='utf-8')
            items = {item.key: item for item in check_environment(
                system_name='Windows',
                version_info=(3, 11, 0, 'final', 0),
                tkinter_available=True,
                runtime_dir=runtime,
            )}
            self.assertEqual(items['people_routing'].status, 'pass')
            self.assertEqual(items['dingtalk_auth'].status, 'pass')
            self.assertEqual(items['ready_gate'].status, 'pass')
            self.assertEqual(items['ledger_mapping'].status, 'fail')
            self.assertFalse(has_gate_failure(items.values()))
            self.assertIn('不读取 ready 文件放行', items['ready_gate'].detail)

    def test_source_does_not_install_or_mutate_system(self):
        text = Path(env_check.__file__).read_text(encoding='utf-8').lower()
        for banned in ('pip install', 'schtasks', 'registry', 'startup', 'winget', 'msiexec'):
            self.assertNotIn(banned, text)


if __name__ == '__main__':
    unittest.main()
