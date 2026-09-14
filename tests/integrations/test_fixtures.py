"""夹具自检：每份样例都标注来源与模拟性质，取值全为合成占位。"""
import importlib.util
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_fixture_loader import fixture_document

REAL_LOOKING = re.compile(
    r'(cid[A-Za-z0-9+/=]{10,}|msg[A-Za-z0-9+/=]{10,}|fld[A-Za-z0-9]{10,}|'
    r'rec[A-Za-z0-9]{10,}|ding[a-z0-9]{16,})'
)


class FixtureLabellingTests(unittest.TestCase):
    def setUp(self):
        self.document = fixture_document()
        self.samples = self.document['samples']

    def test_document_declares_synthetic_scope(self):
        self.assertIn('合成', self.document['_warning'])
        self.assertIn('docs/evidence/', self.document['_scope'])

    def test_every_sample_cites_source_and_observation(self):
        self.assertTrue(self.samples)
        for name, entry in self.samples.items():
            with self.subTest(sample=name):
                self.assertTrue(entry['_synthetic'])
                self.assertIn('docs/evidence/', entry['_source'])
                self.assertTrue(entry['_observed'].strip())
                self.assertIsInstance(entry['payload'], dict)

    def test_identifiers_are_marked_synthetic(self):
        for name, entry in self.samples.items():
            with self.subTest(sample=name):
                for value in _strings(entry['payload']):
                    if REAL_LOOKING.search(value):
                        self.fail(f'{name} 含疑似真实标识: {value!r}')

    def test_identifier_values_carry_the_synthetic_prefix(self):
        for name, entry in self.samples.items():
            with self.subTest(sample=name):
                for key, value in _pairs(entry['payload']):
                    if key.endswith(('Id', 'Ids')) and isinstance(value, str):
                        self.assertTrue(
                            value.startswith('SYNTHETIC-'),
                            f'{name}.{key} 未标为合成: {value!r}',
                        )


class HelperLoadingTests(unittest.TestCase):
    def test_helper_is_not_named_support(self):
        here = Path(__file__).resolve().parent
        self.assertTrue((here / 't03_fixture_loader.py').is_file())
        self.assertFalse((here / 'support.py').exists())
        for path in here.glob('test_*.py'):
            for line in path.read_text(encoding='utf-8').splitlines():
                stripped = line.strip()
                if stripped.startswith('from support import') or stripped == 'import support':
                    self.fail(f'{path.name} still imports generic support')

    def test_helper_loads_by_file_location_without_dir_on_path(self):
        path = Path(__file__).resolve().parent / 't03_fixture_loader.py'
        here = str(path.parent)
        saved = list(sys.path)
        sys.path[:] = [item for item in sys.path if Path(item).resolve() != Path(here).resolve()]
        try:
            spec = importlib.util.spec_from_file_location('_t03_loader_probe', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertIn('record_query_single_page', module.fixture_document()['samples'])
        finally:
            sys.path[:] = saved


def _strings(node):
    for _, value in _pairs(node):
        if isinstance(value, str):
            yield value


def _pairs(node, key=''):
    if isinstance(node, dict):
        for child_key, child in node.items():
            yield from _pairs(child, child_key)
    elif isinstance(node, list):
        for child in node:
            yield from _pairs(child, key)
    else:
        yield key, node


if __name__ == '__main__':
    unittest.main()
