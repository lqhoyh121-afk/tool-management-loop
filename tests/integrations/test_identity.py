import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.dingtalk.errors import IdentityNamespaceError
from integrations.dingtalk.identity import CONTACT, RECORD_CREATOR, TODO, PersonRef


class PersonRefTests(unittest.TestCase):
    def test_namespace_must_be_known(self):
        with self.assertRaises(IdentityNamespaceError):
            PersonRef('directory', 'SYNTHETIC-contact-a')

    def test_value_must_be_non_empty_string(self):
        with self.assertRaises(IdentityNamespaceError):
            PersonRef(CONTACT, '')

    def test_equality_includes_namespace(self):
        self.assertNotEqual(
            PersonRef(CONTACT, 'SYNTHETIC-shared-value'),
            PersonRef(TODO, 'SYNTHETIC-shared-value'),
        )

    def test_same_person_across_namespaces_raises_instead_of_false(self):
        contact = PersonRef(CONTACT, 'SYNTHETIC-shared-value')
        todo = PersonRef(TODO, 'SYNTHETIC-shared-value')
        with self.assertRaises(IdentityNamespaceError):
            contact.same_person_as(todo)

    def test_same_person_within_namespace(self):
        self.assertTrue(
            PersonRef(TODO, '9000000002').same_person_as(
                PersonRef(TODO, '9000000002')
            )
        )
        self.assertFalse(
            PersonRef(TODO, '9000000002').same_person_as(
                PersonRef(TODO, '9000000001')
            )
        )

    def test_record_creator_requires_org(self):
        with self.assertRaises(IdentityNamespaceError):
            PersonRef(RECORD_CREATOR, 'SYNTHETIC-contact-applicant')

    def test_same_person_keeps_org(self):
        left = PersonRef(RECORD_CREATOR, 'SYNTHETIC-contact-applicant', 'SYNTHETIC-corp-0001')
        same = PersonRef(RECORD_CREATOR, 'SYNTHETIC-contact-applicant', 'SYNTHETIC-corp-0001')
        other_org = PersonRef(RECORD_CREATOR, 'SYNTHETIC-contact-applicant', 'SYNTHETIC-corp-0002')
        self.assertTrue(left.same_person_as(same))
        self.assertFalse(left.same_person_as(other_org))
        self.assertNotEqual(left, other_org)

    def test_contact_and_todo_reject_org(self):
        with self.assertRaises(IdentityNamespaceError):
            PersonRef(CONTACT, 'SYNTHETIC-contact-a', 'SYNTHETIC-corp-0001')
        with self.assertRaises(IdentityNamespaceError):
            PersonRef(TODO, '9000000002', 'SYNTHETIC-corp-0001')

    def test_require_rejects_other_namespace(self):
        creator = PersonRef(RECORD_CREATOR, 'SYNTHETIC-contact-applicant', 'SYNTHETIC-corp-0001')
        self.assertIs(creator.require(RECORD_CREATOR), creator)
        with self.assertRaises(IdentityNamespaceError):
            creator.require(CONTACT)

    def test_org_is_kept_only_where_observed(self):
        self.assertIsNone(PersonRef(TODO, 'SYNTHETIC-todo-executor').org)


if __name__ == '__main__':
    unittest.main()
