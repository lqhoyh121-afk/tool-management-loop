import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ROOT = Path(__file__).resolve().parents[2]
BAT = ROOT / 'bootstrap' / 'start.bat'


class LauncherTests(unittest.TestCase):
    def test_bat_exists_and_stays_in_foreground(self):
        text = BAT.read_text(encoding='utf-8')
        self.assertIn('python -m bootstrap', text)
        self.assertIn('py -3 -m bootstrap', text)
        lowered = text.lower()
        self.assertNotIn('start /b', lowered)
        self.assertNotIn('pythonw', lowered)
        self.assertNotIn('schtasks', lowered)
        self.assertIn('pause', lowered)
        self.assertIn('window stays open', lowered)
        self.assertIn('no background business process', lowered)

    def test_no_hidden_worker_modules(self):
        names = {path.name.lower() for path in (ROOT / 'bootstrap').iterdir() if path.is_file()}
        self.assertNotIn('worker.py', names)
        self.assertNotIn('daemon.py', names)
        self.assertNotIn('service.py', names)


if __name__ == '__main__':
    unittest.main()
