import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_fixture_loader import sample

from integrations.dingtalk.envelope import (
    extract_records,
    read_envelope,
    record_cells,
    record_id,
)
from integrations.dingtalk.errors import (
    BusinessErrorResponse,
    MissingFieldError,
    UnknownResultError,
    UnsupportedShapeError,
)


def ok_payload(**extra):
    payload = {'success': True, 'status': 'success', 'error': {}}
    payload.update(extra)
    return payload


class EnvelopeTests(unittest.TestCase):
    def test_plain_success_passes_through(self):
        payload = ok_payload(data={'records': []})
        self.assertIs(read_envelope(payload), payload)

    def test_empty_error_object_is_success_not_missing_code(self):
        payload = ok_payload()
        self.assertIs(read_envelope(payload), payload)

    def test_missing_payload_is_unknown_not_failure(self):
        with self.assertRaises(UnknownResultError):
            read_envelope(None)

    def test_success_true_with_business_error_is_not_success(self):
        with self.assertRaises(BusinessErrorResponse) as caught:
            read_envelope(sample('base_not_found_business_error'))
        self.assertEqual(caught.exception.code, 'BASE_NOT_FOUND')

    def test_error_object_without_code_is_refused(self):
        with self.assertRaises(UnsupportedShapeError):
            read_envelope({'success': True, 'status': 'error', 'error': {'message': 'x'}})

    def test_empty_error_with_status_error_is_contradictory(self):
        with self.assertRaises(UnsupportedShapeError):
            read_envelope({'success': True, 'status': 'error', 'error': {}})

    def test_status_error_without_error_object_is_refused(self):
        with self.assertRaises(UnsupportedShapeError):
            read_envelope({'success': True, 'status': 'error'})

    def test_unobserved_status_is_refused(self):
        with self.assertRaises(UnsupportedShapeError):
            read_envelope({'success': True, 'status': 'partial', 'error': {}})

    def test_missing_status_is_not_success(self):
        with self.assertRaises(UnsupportedShapeError):
            read_envelope({'success': True, 'error': {}})

    def test_missing_error_is_not_success(self):
        with self.assertRaises(UnsupportedShapeError):
            read_envelope({'success': True, 'status': 'success'})

    def test_error_code_envelope_is_refused_even_with_empty_records(self):
        with self.assertRaises(UnsupportedShapeError):
            extract_records(ok_payload(errorCode='SYNTHETIC-ERR', records=[]))

    def test_error_msg_envelope_is_refused(self):
        with self.assertRaises(UnsupportedShapeError):
            read_envelope(ok_payload(errorMsg='SYNTHETIC-message'))

    def test_non_object_payload_is_refused(self):
        with self.assertRaises(UnsupportedShapeError):
            read_envelope([{'success': True}])


class RecordListTests(unittest.TestCase):
    def test_single_page_uses_data_records(self):
        records = extract_records(sample('record_query_single_page'))
        self.assertEqual([record_id(r) for r in records], ['SYNTHETIC-record-0001'])

    def test_all_pages_uses_top_level_records(self):
        records = extract_records(sample('record_query_all_pages'))
        self.assertEqual([record_id(r) for r in records], ['SYNTHETIC-record-0002'])

    def test_null_records_with_no_more_pages_is_empty(self):
        self.assertEqual(extract_records(sample('record_query_empty_all')), [])

    def test_null_records_with_more_pages_is_refused(self):
        payload = sample('record_query_empty_all')
        payload['hasMore'] = True
        with self.assertRaises(UnsupportedShapeError):
            extract_records(payload)

    def test_null_records_missing_has_more_is_refused(self):
        payload = sample('record_query_empty_all')
        del payload['hasMore']
        with self.assertRaises(UnsupportedShapeError):
            extract_records(payload)

    def test_null_records_with_falsy_non_false_has_more_is_refused(self):
        payload = sample('record_query_empty_all')
        payload['hasMore'] = 0
        with self.assertRaises(UnsupportedShapeError):
            extract_records(payload)

    def test_business_error_beats_record_extraction(self):
        with self.assertRaises(BusinessErrorResponse):
            extract_records(sample('base_not_found_business_error'))

    def test_timeout_during_query_is_unknown(self):
        with self.assertRaises(UnknownResultError):
            extract_records(None)

    def test_records_of_wrong_type_are_refused(self):
        with self.assertRaises(UnsupportedShapeError):
            extract_records(ok_payload(records={'recordId': 'x'}))

    def test_record_item_of_wrong_type_is_refused(self):
        with self.assertRaises(UnsupportedShapeError):
            extract_records(ok_payload(records=['SYNTHETIC-record-0003']))

    def test_payload_without_any_records_key_is_refused(self):
        with self.assertRaises(UnsupportedShapeError):
            extract_records(ok_payload(data={'total': 0}))

    def test_missing_record_id_is_reported(self):
        record = extract_records(sample('record_query_single_page'))[0]
        del record['recordId']
        with self.assertRaises(MissingFieldError):
            record_id(record)

    def test_missing_cells_is_reported(self):
        record = extract_records(sample('record_query_single_page'))[0]
        del record['cells']
        with self.assertRaises(MissingFieldError):
            record_cells(record)

    def test_cells_of_wrong_type_are_refused(self):
        with self.assertRaises(UnsupportedShapeError):
            record_cells({'recordId': 'SYNTHETIC-record-0004', 'cells': []})


if __name__ == '__main__':
    unittest.main()
