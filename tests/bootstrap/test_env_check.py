import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.env_check import PENDING_T02, check_environment, format_report, has_blocking_failure
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
        self.assertFalse(has_blocking_failure(items.values()))

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

    def test_unfrozen_items_stay_pending(self):
        items = check_environment(system_name='Windows', version_info=(3, 12, 0, 'final', 0), tkinter_available=True)
        pending = [item for item in items if item.status == 'pending']
        self.assertEqual(
            {item.key for item in pending},
            {'dingtalk_auth', 'people_routing', 'ledger_mapping', 'ready_gate'},
        )
        report = format_report(items)
        self.assertIn(PENDING_T02, report)
        self.assertNotIn('钉钉授权: 通过', report)
        self.assertIn('待确认项不得报告为已满足', report)

    def test_source_does_not_install_or_mutate_system(self):
        text = Path(env_check.__file__).read_text(encoding='utf-8').lower()
        for banned in ('pip install', 'schtasks', 'registry', 'startup', 'winget', 'msiexec'):
            self.assertNotIn(banned, text)


if __name__ == '__main__':
    unittest.main()
