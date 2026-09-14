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


class AssignmentTests(unittest.TestCase):
    def test_python_assignment_flagged(self):
        q = chr(39)
        data = ('API' + '_KEY = ' + q + 'z' * 24 + q + '\n').encode()
        self.assertIn('plaintext secret assignment', inspect('a.py', data))

    def test_yaml_style_assignment_flagged(self):
        q = chr(39)
        data = ('client-secret' + ': ' + q + 'value12345678' + q + '\n').encode()
        self.assertIn('plaintext secret assignment', inspect('conf.yml', data))

    def test_export_assignment_flagged(self):
        q = chr(34)
        data = ('export DB_' + 'TOKEN=' + q + 'abcdef987654' + q + '\n').encode()
        self.assertIn('plaintext secret assignment', inspect('setup.sh', data))

    def test_placeholder_allowed(self):
        q = chr(39)
        data = ('API' + '_KEY = ' + q + 'xxxx-xxxx-xxxx' + q + '\n').encode()
        self.assertEqual(inspect('a.py', data), [])

    def test_angle_placeholder_allowed(self):
        q = chr(39)
        data = ('API' + '_KEY = ' + q + '<your-key-here>' + q + '\n').encode()
        self.assertEqual(inspect('a.py', data), [])

    def test_env_reference_allowed(self):
        data = ('TO' + 'KEN = os.environ[' + chr(39) + 'TO' + 'KEN' + chr(39) + ']\n').encode()
        self.assertEqual(inspect('a.py', data), [])

    def test_non_ascii_value_allowed(self):
        q = chr(39)
        data = ('API' + '_KEY = ' + q + '这里只是中文说明不是密钥' + q + '\n').encode()
        self.assertEqual(inspect('a.py', data), [])

    def test_non_secret_name_allowed(self):
        q = chr(39)
        data = ('keyboard = ' + q + 'ctrl+shift+p' + q + '\n').encode()
        self.assertEqual(inspect('a.py', data), [])

    def test_short_value_allowed(self):
        q = chr(39)
        data = ('to' + 'ken = ' + q + 'abc' + q + '\n').encode()
        self.assertEqual(inspect('a.py', data), [])


class ArtifactTests(unittest.TestCase):
    def test_dev_flow_blocked(self):
        self.assertIn('private/runtime artifact', inspect('.dev-flow/state.md', b'x'))

    def test_runtime_dir_blocked(self):
        self.assertIn('private/runtime artifact', inspect('runtime/state.json', b'x'))

    def test_local_private_blocked(self):
        self.assertIn('private/runtime artifact', inspect('local-private/notes.txt', b'x'))

    def test_log_suffix_blocked(self):
        self.assertIn('private/runtime artifact', inspect('run.log', b'x'))

    def test_handover_name_blocked(self):
        self.assertIn('local handover/receipt artifact', inspect('notes/handover_note.md', b'x'))

    def test_receipt_name_blocked(self):
        self.assertIn('local handover/receipt artifact', inspect('docs/receipt_2026.txt', b'x'))

    def test_chinese_handover_blocked(self):
        self.assertIn('local handover/receipt artifact', inspect('docs/交接说明.md', b'x'))

    def test_local_override_blocked(self):
        self.assertIn('local config override', inspect('config.local.yml', b'x'))


if __name__ == '__main__':
    unittest.main()
