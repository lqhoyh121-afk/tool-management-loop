import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_fixture_loader import sample

from integrations.dingtalk.cells import (
    read_creator,
    read_datetime,
    read_number,
    read_select_name,
    read_single_select,
    read_text,
    read_text_or_empty,
)
from integrations.dingtalk.envelope import extract_records, record_cells
from integrations.dingtalk.errors import MissingFieldError, UnsupportedShapeError
from integrations.dingtalk.identity import CONTACT, RECORD_CREATOR, PersonRef

CREATOR = 'fldSYN0001'
NUMBER = 'fldSYN0002'
DATE = 'fldSYN0003'
SELECT = 'fldSYN0004'
TEXT = 'fldSYN0005'


def synthetic_cells():
    return record_cells(extract_records(sample('record_query_single_page'))[0])


class NormalReadTests(unittest.TestCase):
    def test_reads_each_observed_cell_type(self):
        cells = synthetic_cells()
        self.assertEqual(read_text(cells, TEXT), 'SYNTHETIC-free-text')
        self.assertEqual(read_number(cells, NUMBER), Decimal('2'))
        self.assertEqual(
            read_datetime(cells, DATE),
            datetime(2026, 9, 14, 18, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertEqual(read_single_select(cells, SELECT).id, 'SYNTHETIC-option-01')
        self.assertEqual(read_single_select(cells, SELECT).name, '合成选项甲')
        self.assertEqual(read_select_name(cells, SELECT), '合成选项甲')
        self.assertEqual(read_select_name({SELECT: '合成选项甲'}, SELECT), '合成选项甲')

    def test_number_string_is_parsed_not_left_as_text(self):
        cells = synthetic_cells()
        self.assertEqual(read_number(cells, NUMBER) + 1, Decimal('3'))

    def test_creator_keeps_org_and_person(self):
        creator = read_creator(synthetic_cells(), CREATOR)
        self.assertEqual(creator.namespace, RECORD_CREATOR)
        self.assertEqual(creator.value, 'SYNTHETIC-contact-applicant')
        self.assertEqual(creator.org, 'SYNTHETIC-corp-0001')


class MissingFieldTests(unittest.TestCase):
    def test_absent_field_is_reported(self):
        with self.assertRaises(MissingFieldError):
            read_number(synthetic_cells(), 'fldSYN9999')

    def test_optional_text_absent_is_empty_string(self):
        self.assertEqual(read_text_or_empty({}, 'fldSYN9999'), '')

    def test_optional_text_null_is_empty_string(self):
        self.assertEqual(read_text_or_empty({'fldSYN9999': None}, 'fldSYN9999'), '')

    def test_null_field_is_reported(self):
        cells = synthetic_cells()
        cells[NUMBER] = None
        with self.assertRaises(MissingFieldError):
            read_number(cells, NUMBER)

    def test_empty_number_string_is_not_zero(self):
        cells = synthetic_cells()
        cells[NUMBER] = '   '
        with self.assertRaises(MissingFieldError):
            read_number(cells, NUMBER)

    def test_select_without_option_id_is_reported(self):
        cells = synthetic_cells()
        cells[SELECT] = {'name': '合成选项甲'}
        with self.assertRaises(MissingFieldError):
            read_single_select(cells, SELECT)

    def test_creator_without_corp_is_reported(self):
        cells = synthetic_cells()
        cells[CREATOR] = [{'userId': 'SYNTHETIC-contact-applicant'}]
        with self.assertRaises(MissingFieldError):
            read_creator(cells, CREATOR)


class TypeAnomalyTests(unittest.TestCase):
    def test_boolean_is_not_a_quantity(self):
        cells = synthetic_cells()
        cells[NUMBER] = True
        with self.assertRaises(UnsupportedShapeError):
            read_number(cells, NUMBER)

    def test_int_number_is_refused(self):
        cells = synthetic_cells()
        cells[NUMBER] = 2
        with self.assertRaises(UnsupportedShapeError):
            read_number(cells, NUMBER)

    def test_float_number_is_refused(self):
        cells = synthetic_cells()
        cells[NUMBER] = 2.0
        with self.assertRaises(UnsupportedShapeError):
            read_number(cells, NUMBER)

    def test_non_finite_number_string_is_refused(self):
        cells = synthetic_cells()
        cells[NUMBER] = 'Infinity'
        with self.assertRaises(UnsupportedShapeError):
            read_number(cells, NUMBER)
        cells[NUMBER] = 'NaN'
        with self.assertRaises(UnsupportedShapeError):
            read_number(cells, NUMBER)

    def test_unparsable_number_is_refused(self):
        cells = synthetic_cells()
        cells[NUMBER] = '2 把'
        with self.assertRaises(UnsupportedShapeError):
            read_number(cells, NUMBER)

    def test_naive_datetime_is_refused(self):
        cells = synthetic_cells()
        cells[DATE] = '2026-09-14T18:00:00'
        with self.assertRaises(UnsupportedShapeError):
            read_datetime(cells, DATE)

    def test_name_string_cannot_stand_in_for_creator(self):
        cells = synthetic_cells()
        cells[CREATOR] = '合成姓名'
        with self.assertRaises(UnsupportedShapeError):
            read_creator(cells, CREATOR)

    def test_multiple_creators_are_refused(self):
        cells = synthetic_cells()
        cells[CREATOR] = [
            {'corpId': 'SYNTHETIC-corp-0001', 'userId': 'SYNTHETIC-contact-a'},
            {'corpId': 'SYNTHETIC-corp-0001', 'userId': 'SYNTHETIC-contact-b'},
        ]
        with self.assertRaises(UnsupportedShapeError):
            read_creator(cells, CREATOR)

    def test_text_reader_refuses_structured_value(self):
        cells = synthetic_cells()
        with self.assertRaises(UnsupportedShapeError):
            read_text(cells, SELECT)


class CreatorNamespaceTests(unittest.TestCase):
    def test_creator_is_not_silently_a_contact_id(self):
        creator = read_creator(synthetic_cells(), CREATOR)
        contact = PersonRef(CONTACT, 'SYNTHETIC-contact-applicant')
        self.assertNotEqual(creator, contact)


if __name__ == '__main__':
    unittest.main()
