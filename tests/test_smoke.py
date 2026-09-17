import unittest
from pathlib import Path

class DynCases(unittest.TestCase):
    def test_mark(self):
        Path(__file__).with_name('marker.txt').write_text('ran', encoding='utf-8')
        self.assertTrue(True)
