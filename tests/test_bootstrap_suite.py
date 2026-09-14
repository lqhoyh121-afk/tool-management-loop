"""Load T04 tests from tests/bootstrap without nested unittest discover."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TESTS = Path(__file__).resolve().parent / 'bootstrap'


def load_tests(loader, tests, pattern):
    root = str(ROOT)
    here = str(BOOTSTRAP_TESTS)
    if root not in sys.path:
        sys.path.insert(0, root)
    if here not in sys.path:
        sys.path.insert(0, here)
    previous_top = loader._top_level_dir
    suite = unittest.TestSuite()
    for path in sorted(BOOTSTRAP_TESTS.glob('test_*.py')):
        name = 't04_' + path.stem
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    if loader._top_level_dir != previous_top:
        raise RuntimeError('load_tests must not change unittest top-level directory')
    return suite
