from dataclasses import replace
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from contracts.model import Code, ContractError, Resource, State
from integrations.dingtalk.codec import decode_inventory, decode_loan, encode_inventory, encode_loan
from integrations.dingtalk.layout import SYNTHETIC_FIELDS

import importlib.util


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fixtures = _load('t03_codec_fixtures', Path(__file__).resolve().parents[2] / 'tests' / 'contracts' / 'fixtures.py')
loan, stock = _fixtures.loan, _fixtures.stock


class CodecTests(unittest.TestCase):
    def test_loan_roundtrip_keeps_t01_cell_shapes(self):
        fields = SYNTHETIC_FIELDS
        current = loan()
        cells = encode_loan(current, fields)
        self.assertIsInstance(cells[fields.quantity], str)
        self.assertIsInstance(cells[fields.due_at], str)
        self.assertEqual(cells[fields.state], current.state.value)
        self.assertEqual(cells[fields.tracked], 'false')
        self.assertIsInstance(cells[fields.state], str)
        self.assertIsInstance(cells[fields.tracked], str)
        self.assertEqual(cells[fields.borrower], [
            {'corpId': current.borrower.tenant_id, 'userId': current.borrower.user_id},
        ])
        cells[fields.state] = {
            'id': 'SYNTHETIC-rand-state', 'name': current.state.value,
        }
        cells[fields.tracked] = {'id': 'SYNTHETIC-rand-tracked', 'name': 'false'}
        decoded = decode_loan(current.ref, cells, fields)
        self.assertEqual(decoded, current)

    def test_inventory_roundtrip(self):
        fields = SYNTHETIC_FIELDS
        current = stock()
        decoded = decode_inventory(current.ref, encode_inventory(current, fields), fields)
        self.assertEqual(decoded, current)

    def test_number_cell_must_be_a_string(self):
        fields = SYNTHETIC_FIELDS
        cells = encode_loan(loan(), fields)
        cells[fields.quantity] = 2
        with self.assertRaises(ContractError) as caught:
            decode_loan(loan().ref, cells, fields)
        self.assertEqual(caught.exception.code, Code.EVIDENCE)

    def test_decode_loan_rejects_bare_string_single_select(self):
        fields = SYNTHETIC_FIELDS
        cells = encode_loan(loan(), fields)
        with self.assertRaises(ContractError) as caught:
            decode_loan(loan().ref, cells, fields)
        self.assertEqual(caught.exception.code, Code.EVIDENCE)

    def test_select_name_not_id_and_omitted_empty_return(self):
        fields = SYNTHETIC_FIELDS
        current = loan()
        cells = encode_loan(current, fields)
        cells[fields.state] = {'id': 'SYNTHETIC-rand-state', 'name': current.state.value}
        cells[fields.tracked] = {'id': 'SYNTHETIC-rand-tracked', 'name': 'false'}
        cells.pop(fields.return_id, None)
        cells.pop(fields.return_container, None)
        self.assertEqual(decode_loan(current.ref, cells, fields), current)

    def test_return_ref_roundtrip(self):
        fields = SYNTHETIC_FIELDS
        current = replace(
            loan(),
            state=State.AWAITING_RETURN,
            return_ref=Resource('record', loan().ref.tenant_id,
                                'synthetic-returns', 'synthetic-return'),
        )
        cells = encode_loan(current, fields)
        cells[fields.state] = {'id': 'SYN-rand', 'name': current.state.value}
        cells[fields.tracked] = {'id': 'SYN-rand', 'name': 'false'}
        decoded = decode_loan(current.ref, cells, fields)
        self.assertEqual(decoded, current)


if __name__ == '__main__':
    unittest.main()
