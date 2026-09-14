"""夹具自检：每份样例都标注来源与模拟性质，取值全为合成占位。"""
import re
import unittest

from support import fixture_document

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
