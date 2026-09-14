"""Load T04 tests from tests/bootstrap so the default discovery command sees them."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TESTS = Path(__file__).resolve().parent / 'bootstrap'


def load_tests(loader, tests, pattern):
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return loader.discover(
        start_dir=str(BOOTSTRAP_TESTS),
        pattern='test_*.py',
        top_level_dir=str(BOOTSTRAP_TESTS),
    )
