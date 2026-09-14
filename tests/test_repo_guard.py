import unittest
from scripts.repo_guard import inspect


class GuardTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(inspect('docs/readme.md', '真实数据不提交。'.encode()), [])

    def test_backup(self):
        self.assertIn('private/runtime artifact', inspect('a.bak', b'x'))

    def test_env(self):
        self.assertIn('environment file', inspect('.env', b'x'))
        self.assertEqual(inspect('.env.example', b'# empty configuration example'), [])

    def test_windows_path(self):
        data = ('C:' + chr(92) + 'Users' + chr(92) + 'example' + chr(92) + 'file').encode()
        self.assertIn('personal absolute path', inspect('a.py', data))

    def test_posix_path(self):
        data = ('/' + 'home' + '/example/file').encode()
        self.assertIn('personal absolute path', inspect('a.md', data))

    def test_token(self):
        self.assertIn('credential-shaped value', inspect('a.txt', ('gh' + 'p_' + 'a' * 30).encode()))

    def test_signature(self):
        self.assertIn('embedded signature', inspect('a.md', ('署' + '名：example').encode()))

    def test_binary(self):
        self.assertTrue(inspect('ledger.xlsx', b'PK\xff'))


if __name__ == '__main__':
    unittest.main()
