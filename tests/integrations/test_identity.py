import unittest

from support import sample  # noqa: F401  # 统一把仓库根加入 sys.path

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
            PersonRef(TODO, 'SYNTHETIC-todo-executor').same_person_as(
                PersonRef(TODO, 'SYNTHETIC-todo-executor')
            )
        )
        self.assertFalse(
            PersonRef(TODO, 'SYNTHETIC-todo-executor').same_person_as(
                PersonRef(TODO, 'SYNTHETIC-todo-creator')
            )
        )

    def test_require_rejects_other_namespace(self):
        creator = PersonRef(RECORD_CREATOR, 'SYNTHETIC-contact-applicant', 'SYNTHETIC-corp-0001')
        self.assertIs(creator.require(RECORD_CREATOR), creator)
        with self.assertRaises(IdentityNamespaceError):
            creator.require(CONTACT)

    def test_org_is_kept_only_where_observed(self):
        self.assertIsNone(PersonRef(TODO, 'SYNTHETIC-todo-executor').org)


if __name__ == '__main__':
    unittest.main()
