import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import repo_checks

GIT = shutil.which('git')


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')


def dummy_test_source():
    return (
        'import unittest\n'
        'from pathlib import Path\n'
        '\n'
        'class DynCases(unittest.TestCase):\n'
        '    def test_mark(self):\n'
        "        Path(__file__).with_name('marker.txt').write_text('ran', encoding='utf-8')\n"
        '        self.assertTrue(True)\n')


def counting_test_source():
    return (
        'import unittest\n'
        'from pathlib import Path\n'
        '\n'
        'class CountCases(unittest.TestCase):\n'
        '    def test_count(self):\n'
        "        target = Path(__file__).with_name('count.txt')\n"
        "        with target.open('a', encoding='utf-8') as handle:\n"
        "            handle.write('x')\n"
        '        self.assertTrue(True)\n')


def adapter_source():
    return (
        '"""Adapter mirroring the real load_tests suite-entry shape."""\n'
        'import importlib.util\n'
        'import sys\n'
        'import unittest\n'
        'from pathlib import Path\n'
        '\n'
        'BOOT = Path(__file__).resolve().parent / "sub"\n'
        '\n'
        '\n'
        'def load_tests(loader, tests, pattern):\n'
        '    suite = unittest.TestSuite()\n'
        '    here = str(BOOT)\n'
        '    if here not in sys.path:\n'
        '        sys.path.insert(0, here)\n'
        '    for path in sorted(BOOT.glob("test_*.py")):\n'
        '        name = "ad_" + path.stem\n'
        '        spec = importlib.util.spec_from_file_location(name, path)\n'
        '        module = importlib.util.module_from_spec(spec)\n'
        '        sys.modules[name] = module\n'
        '        spec.loader.exec_module(module)\n'
        '        suite.addTests(loader.loadTestsFromModule(module))\n'
        '    return suite\n')


def adapter_import_only_source():
    return (
        '"""Adapter that imports a test file for helpers but never collects it."""\n'
        'import importlib.util\n'
        'import sys\n'
        'import unittest\n'
        'from pathlib import Path\n'
        '\n'
        'TARGET = Path(__file__).resolve().parent / "sub" / "test_hidden.py"\n'
        '\n'
        '\n'
        'class AdapterCases(unittest.TestCase):\n'
        '    def test_adapter(self):\n'
        '        self.assertTrue(True)\n'
        '\n'
        '\n'
        'def load_tests(loader, tests, pattern):\n'
        '    name = "ad_hidden"\n'
        '    spec = importlib.util.spec_from_file_location(name, TARGET)\n'
        '    module = importlib.util.module_from_spec(spec)\n'
        '    sys.modules[name] = module\n'
        '    spec.loader.exec_module(module)\n'
        '    suite = unittest.TestSuite()\n'
        '    suite.addTests(loader.loadTestsFromTestCase(AdapterCases))\n'
        '    return suite\n')


def adapter_partial_source():
    return (
        '"""Adapter that imports a test file and collects only one case."""\n'
        'import importlib.util\n'
        'import sys\n'
        'import unittest\n'
        'from pathlib import Path\n'
        '\n'
        'TARGET = Path(__file__).resolve().parent / "sub" / "test_two.py"\n'
        '\n'
        '\n'
        'def load_tests(loader, tests, pattern):\n'
        '    name = "ad_two"\n'
        '    spec = importlib.util.spec_from_file_location(name, TARGET)\n'
        '    module = importlib.util.module_from_spec(spec)\n'
        '    sys.modules[name] = module\n'
        '    spec.loader.exec_module(module)\n'
        '    suite = unittest.TestSuite()\n'
        '    suite.addTest(module.TwoCases("test_first"))\n'
        '    return suite\n')


def secret_source():
    q = chr(39)
    return 'API' + '_KEY = ' + q + 'z' * 24 + q + '\n'


def run_cmd(args, cwd, timeout=180):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout, env=env)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tests_dir = Path(self.tmp.name) / 'tests'

    def run_runner(self):
        stream = io.StringIO()
        problems, stats = repo_checks.run_tests(tests_dir=self.tests_dir, stream=stream)
        return problems, stats

    def test_nested_without_init_package_runs(self):
        write(self.tests_dir / 'nested' / 'test_marker.py', dummy_test_source())
        problems, stats = self.run_runner()
        self.assertEqual(problems, [])
        self.assertEqual(stats['files'], 1)
        self.assertEqual(stats['tests'], 1)
        self.assertTrue((self.tests_dir / 'nested' / 'marker.txt').exists())

    def test_same_name_in_two_dirs_both_run(self):
        write(self.tests_dir / 'a' / 'test_dup.py', dummy_test_source())
        write(self.tests_dir / 'b' / 'test_dup.py', dummy_test_source())
        problems, stats = self.run_runner()
        self.assertEqual(problems, [])
        self.assertEqual(stats['files'], 2)
        self.assertEqual(stats['tests'], 2)
        self.assertTrue((self.tests_dir / 'a' / 'marker.txt').exists())
        self.assertTrue((self.tests_dir / 'b' / 'marker.txt').exists())

    def test_non_identifier_dirs_run(self):
        write(self.tests_dir / 'sub-dir' / 'test_x.py', dummy_test_source())
        write(self.tests_dir / '子目录' / 'test_y.py', dummy_test_source())
        problems, stats = self.run_runner()
        self.assertEqual(problems, [])
        self.assertEqual(stats['tests'], 2)

    def test_no_test_files_fails(self):
        self.tests_dir.mkdir(parents=True)
        problems, stats = self.run_runner()
        self.assertIn('no test files', problems)

    def test_zero_cases_fails(self):
        write(self.tests_dir / 'test_empty.py', 'VALUE = 1\n')
        problems, stats = self.run_runner()
        self.assertIn('zero test cases', problems)

    def test_import_error_reports_file(self):
        write(self.tests_dir / 'test_broken.py', 'import nonexistent_module_marker\n')
        problems, stats = self.run_runner()
        joined = '\n'.join(problems)
        self.assertIn('test_broken.py', joined)
        self.assertIn('load failed', joined)

    def test_failing_test_reports_problem(self):
        write(self.tests_dir / 'test_fails.py',
              'import unittest\n\n\nclass FailCases(unittest.TestCase):\n'
              '    def test_fail(self):\n'
              '        self.assertTrue(False)\n')
        problems, stats = self.run_runner()
        self.assertTrue(any('test failure' in p for p in problems), problems)

    def test_adapter_files_run_exactly_once(self):
        write(self.tests_dir / 'sub' / 'test_one.py', counting_test_source())
        write(self.tests_dir / 'sub' / 'test_two.py', counting_test_source())
        write(self.tests_dir / 'test_suite.py', adapter_source())
        write(self.tests_dir / 'test_plain.py', counting_test_source())
        problems, stats = self.run_runner()
        self.assertEqual(problems, [])
        self.assertEqual(stats['tests'], 3)
        self.assertEqual((self.tests_dir / 'sub' / 'count.txt').read_text(
            encoding='utf-8'), 'xx')
        self.assertEqual((self.tests_dir / 'count.txt').read_text(encoding='utf-8'), 'x')

    def test_adapter_load_failure_reported(self):
        write(self.tests_dir / 'sub' / 'test_one.py', counting_test_source())
        write(self.tests_dir / 'test_suite.py',
              'import unittest\n\n\ndef load_tests(loader, tests, pattern):\n'
              '    raise RuntimeError("adapter exploded")\n')
        stream = io.StringIO()
        problems, stats = repo_checks.run_tests(tests_dir=self.tests_dir, stream=stream)
        joined = '\n'.join(problems)
        self.assertIn('test_suite.py', joined)
        self.assertIn('load failed', joined)
        self.assertIn('RuntimeError', stream.getvalue())

    def test_adapter_import_without_collection_still_runs(self):
        write(self.tests_dir / 'sub' / 'test_hidden.py',
              'import unittest\n\n\nclass HiddenCases(unittest.TestCase):\n'
              '    def test_hidden_must_fail(self):\n'
              '        self.fail("never collected by adapter")\n')
        write(self.tests_dir / 'test_suite.py', adapter_import_only_source())
        problems, stats = self.run_runner()
        self.assertEqual(stats['tests'], 2)
        self.assertEqual(stats['failures'], 1)
        self.assertTrue(any('test failure' in p for p in problems), problems)

    def test_partial_adapter_coverage_fails_closed(self):
        write(self.tests_dir / 'sub' / 'test_two.py',
              'import unittest\n\n\nclass TwoCases(unittest.TestCase):\n'
              '    def test_first(self):\n'
              '        self.assertTrue(True)\n'
              '    def test_second(self):\n'
              '        self.assertTrue(True)\n')
        write(self.tests_dir / 'test_suite.py', adapter_partial_source())
        problems, stats = self.run_runner()
        joined = '\n'.join(problems)
        self.assertIn('test_two.py', joined)
        self.assertIn('failing closed', joined)

    def test_two_adapters_collecting_same_file_flagged(self):
        write(self.tests_dir / 'sub' / 'test_one.py', counting_test_source())
        write(self.tests_dir / 'test_suite_a.py', adapter_source())
        write(self.tests_dir / 'test_suite_b.py', adapter_source())
        problems, stats = self.run_runner()
        self.assertTrue(any('duplicate test case execution' in p for p in problems), problems)

    def test_rerun_is_idempotent(self):
        write(self.tests_dir / 'test_once.py', dummy_test_source())
        first = self.run_runner()
        second = self.run_runner()
        self.assertEqual(first, second)


class FileCheckTests(unittest.TestCase):
    def test_syntax_error_reported_with_line(self):
        self.assertTrue(any('syntax error line 1' in i
                            for i in repo_checks.syntax_issues('a.py', b'def broken(:\n')))

    def test_syntax_ok(self):
        self.assertEqual(repo_checks.syntax_issues('a.py', b'x = 1\n'), [])

    def test_trailing_whitespace_python(self):
        issues = repo_checks.format_issues('a.py', b'x = 1  \n')
        self.assertIn('trailing whitespace line 1', issues)

    def test_trailing_whitespace_yaml(self):
        issues = repo_checks.format_issues('a.yml', b'key: value  \n')
        self.assertIn('trailing whitespace line 1', issues)

    def test_markdown_whitespace_exempt(self):
        self.assertEqual(repo_checks.format_issues('a.md', b'text  \nmore\n'), [])

    def test_missing_final_newline(self):
        for name in ('a.py', 'a.md', 'a.yml'):
            self.assertIn('missing final newline', repo_checks.format_issues(name, b'x'))

    def test_final_newline_present(self):
        self.assertEqual(repo_checks.format_issues('a.py', b'x\n'), [])

    def test_crlf_python_clean(self):
        self.assertEqual(repo_checks.format_issues('a.py', b'x = 1\r\ny = 2\r\n'), [])

    def test_crlf_python_real_spaces_flagged(self):
        issues = repo_checks.format_issues('a.py', b'x = 1  \r\n')
        self.assertIn('trailing whitespace line 1', issues)

    def test_bat_never_format_checked(self):
        self.assertEqual(repo_checks.format_issues('run.bat', b'@echo off  \r\nx'), [])

    def test_non_utf8_skips_text_layer(self):
        self.assertEqual(repo_checks.format_issues('a.py', b'\xff\xfe\x00'), [])

    def test_staged_content_checked_not_worktree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        (repo / 'scripts').mkdir()
        for name in ('repo_guard.py', 'repo_checks.py'):
            shutil.copy2(REPO_ROOT / 'scripts' / name, repo / 'scripts' / name)
        write(repo / 'tests' / 'test_smoke.py', dummy_test_source())
        run_cmd(['git', 'init', '-q', '-b', 'main'], repo)
        run_cmd(['git', 'config', 'user.email', 'synth@example.invalid'], repo)
        run_cmd(['git', 'config', 'user.name', 'synth-tester'], repo)
        write(repo / 'stage_me.py', secret_source())
        run_cmd(['git', 'add', 'stage_me.py'], repo)
        write(repo / 'stage_me.py', 'VALUE = 1\n')
        staged = run_cmd([sys.executable, 'scripts/repo_checks.py', '--staged'], repo)
        self.assertEqual(staged.returncode, 1, staged.stdout + staged.stderr)
        self.assertIn('plaintext secret assignment', staged.stdout)
        run_cmd(['git', 'add', 'stage_me.py'], repo)
        full = run_cmd([sys.executable, 'scripts/repo_checks.py'], repo)
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)


class CliOutcomeTests(unittest.TestCase):
    def run_cli(self, test_sources):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        (repo / 'scripts').mkdir()
        for name in ('repo_guard.py', 'repo_checks.py'):
            shutil.copy2(REPO_ROOT / 'scripts' / name, repo / 'scripts' / name)
        for rel, source in test_sources.items():
            write(repo / 'tests' / rel, source)
        run_cmd(['git', 'init', '-q', '-b', 'main'], repo)
        run_cmd(['git', 'config', 'user.email', 'synth@example.invalid'], repo)
        run_cmd(['git', 'config', 'user.name', 'synth-tester'], repo)
        run_cmd(['git', 'add', 'scripts', 'tests'], repo)
        return run_cmd([sys.executable, 'scripts/repo_checks.py'], repo)

    def test_assertion_failure_exits_nonzero(self):
        proc = self.run_cli({'test_a.py': 'import unittest\n\n\nclass A(unittest.TestCase):\n'
                                          '    def test_a(self):\n'
                                          '        self.fail("boom")\n'})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('RESULT: FAIL', proc.stdout)
        self.assertIn('AssertionError', proc.stderr)

    def test_runtime_error_exits_nonzero(self):
        proc = self.run_cli({'test_a.py': 'import unittest\n\n\nclass A(unittest.TestCase):\n'
                                          '    def test_a(self):\n'
                                          '        raise RuntimeError("boom")\n'})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('RuntimeError', proc.stderr)

    def test_fixture_error_exits_nonzero(self):
        proc = self.run_cli({'test_a.py': 'import unittest\n\n\nclass A(unittest.TestCase):\n'
                                          '    def setUp(self):\n'
                                          '        raise ValueError("broken fixture")\n'
                                          '    def test_a(self):\n'
                                          '        self.assertTrue(True)\n'})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ValueError', proc.stderr)

    def test_load_tests_error_exits_nonzero(self):
        proc = self.run_cli({'test_a.py': 'import unittest\n\n\ndef load_tests(loader, tests, pattern):\n'
                                          '    raise RuntimeError("broken adapter")\n'})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('RESULT: FAIL', proc.stdout)


@unittest.skipUnless(GIT, 'git not available')
class HookAndInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / 'scripts').mkdir()
        (self.repo / '.githooks').mkdir()
        for name in ('repo_guard.py', 'repo_checks.py', 'install_hooks.py'):
            shutil.copy2(REPO_ROOT / 'scripts' / name, self.repo / 'scripts' / name)
        shutil.copy2(REPO_ROOT / '.githooks' / 'pre-commit', self.repo / '.githooks' / 'pre-commit')
        write(self.repo / 'tests' / 'test_smoke.py', dummy_test_source())
        run_cmd(['git', 'init', '-q', '-b', 'main'], self.repo)
        run_cmd(['git', 'config', 'user.email', 'synth@example.invalid'], self.repo)
        run_cmd(['git', 'config', 'user.name', 'synth-tester'], self.repo)

    def local_hooks_path(self):
        proc = run_cmd(['git', 'config', '--local', '--get', 'core.hooksPath'], self.repo)
        return proc.stdout.strip() if proc.returncode == 0 else ''

    def test_install_sets_local_hookspath(self):
        proc = run_cmd([sys.executable, 'scripts/install_hooks.py'], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.local_hooks_path(), '.githooks')

    def test_install_refuses_different_hookspath(self):
        run_cmd(['git', 'config', '--local', 'core.hooksPath', 'other-hooks'], self.repo)
        proc = run_cmd([sys.executable, 'scripts/install_hooks.py'], self.repo)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.local_hooks_path(), 'other-hooks')

    def test_global_hookspath_untouched(self):
        before = run_cmd(['git', 'config', '--global', '--get', 'core.hooksPath'], self.repo)
        run_cmd([sys.executable, 'scripts/install_hooks.py'], self.repo)
        after = run_cmd(['git', 'config', '--global', '--get', 'core.hooksPath'], self.repo)
        self.assertEqual((before.returncode, before.stdout), (after.returncode, after.stdout))

    def baseline_commit(self):
        run_cmd(['git', 'add', 'scripts', '.githooks', 'tests'], self.repo)
        proc = run_cmd(['git', 'commit', '-q', '-m', 'init'], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return run_cmd(['git', 'rev-parse', 'HEAD'], self.repo).stdout.strip()

    def test_precommit_blocks_secret_and_keeps_head(self):
        run_cmd([sys.executable, 'scripts/install_hooks.py'], self.repo)
        head = self.baseline_commit()
        write(self.repo / 'secret.py', secret_source())
        run_cmd(['git', 'add', 'secret.py'], self.repo)
        proc = run_cmd(['git', 'commit', '-q', '-m', 'bad'], self.repo)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('plaintext secret assignment', proc.stdout + proc.stderr)
        self.assertEqual(run_cmd(['git', 'rev-parse', 'HEAD'], self.repo).stdout.strip(), head)

    def test_precommit_blocks_failing_test_and_keeps_head(self):
        run_cmd([sys.executable, 'scripts/install_hooks.py'], self.repo)
        head = self.baseline_commit()
        write(self.repo / 'tests' / 'test_bad.py',
              'import unittest\n\n\nclass BadCases(unittest.TestCase):\n'
              '    def test_bad(self):\n'
              '        self.fail("broken")\n')
        run_cmd(['git', 'add', 'tests/test_bad.py'], self.repo)
        proc = run_cmd(['git', 'commit', '-q', '-m', 'bad test'], self.repo)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(run_cmd(['git', 'rev-parse', 'HEAD'], self.repo).stdout.strip(), head)

    def test_precommit_allows_clean_commit(self):
        run_cmd([sys.executable, 'scripts/install_hooks.py'], self.repo)
        head = self.baseline_commit()
        write(self.repo / 'note.txt', 'clean note\n')
        run_cmd(['git', 'add', 'note.txt'], self.repo)
        proc = run_cmd(['git', 'commit', '-q', '-m', 'good'], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotEqual(run_cmd(['git', 'rev-parse', 'HEAD'], self.repo).stdout.strip(), head)

    def test_invocation_from_non_root_cwd(self):
        notes = self.repo / 'notes'
        notes.mkdir()
        write(self.repo / 'clean.py', 'VALUE = 1\n')
        run_cmd(['git', 'add', 'clean.py'], self.repo)
        proc = run_cmd([sys.executable, str(self.repo / 'scripts' / 'repo_checks.py'),
                        '--staged'], notes)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('PASS', proc.stdout)


if __name__ == '__main__':
    unittest.main()
