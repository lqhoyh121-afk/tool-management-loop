"""Single check entry for the pre-commit hook and CI: file gates plus tests."""
from pathlib import Path
import argparse
import ast
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

try:
    from scripts.repo_guard import inspect
except ImportError:  # executed as a script: scripts/ is importable, root is not
    from repo_guard import inspect

SYNTAX_SUFFIX = '.py'
WHITESPACE_SUFFIXES = {'.py', '.yml', '.yaml'}
FINAL_NEWLINE_SUFFIXES = {'.py', '.md', '.yml', '.yaml'}


def tracked_paths(staged=False):
    args = ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'] if staged else ['git', 'ls-files', '-z']
    out = subprocess.check_output(args, cwd=ROOT).decode('utf-8')
    return [p for p in out.split('\0') if p]


def file_bytes(path, staged=False):
    if staged:
        return subprocess.check_output(['git', 'show', ':' + path], cwd=ROOT)
    return (ROOT / path).read_bytes()


def syntax_issues(path, data):
    if Path(path).suffix != SYNTAX_SUFFIX:
        return []
    try:
        ast.parse(data, filename=path)
    except SyntaxError as exc:
        return [f'syntax error line {exc.lineno}: {exc.msg}']
    except ValueError as exc:
        return [f'syntax error: {exc}']
    return []


def format_issues(path, data):
    suffix = Path(path).suffix.lower()
    if suffix not in WHITESPACE_SUFFIXES and suffix not in FINAL_NEWLINE_SUFFIXES:
        return []
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return []  # already reported by inspect as non-UTF8
    issues = []
    if suffix in WHITESPACE_SUFFIXES:
        for lineno, line in enumerate(text.splitlines(), 1):
            if line != line.rstrip(' \t'):
                issues.append(f'trailing whitespace line {lineno}')
    if suffix in FINAL_NEWLINE_SUFFIXES and data and not data.endswith(b'\n'):
        issues.append('missing final newline')
    return issues


def check_files(staged=False):
    paths = tracked_paths(staged)
    if not paths:
        if staged:
            print('No staged files to check.')
            return []
        print('ERROR: no tracked files')
        return ['no tracked files']
    failures = []
    for path in paths:
        try:
            data = file_bytes(path, staged)
        except subprocess.CalledProcessError:
            failures.append(f'{path}: cannot read staged content')
            continue
        except OSError:
            failures.append(f'{path}: tracked file missing from worktree')
            continue
        for problem in inspect(path, data) + syntax_issues(path, data) + format_issues(path, data):
            failures.append(f'{path}: {problem}')
    print('\n'.join(failures) if failures else f'PASS: checked {len(paths)} files')
    return failures


def collect_test_files(tests_dir):
    found = []
    for dirpath, dirnames, filenames in os.walk(tests_dir):
        dirnames[:] = sorted(d for d in dirnames if d != '__pycache__')
        for name in sorted(filenames):
            if name.startswith('test_') and name.endswith('.py'):
                full = Path(dirpath) / name
                found.append((full, full.relative_to(tests_dir).as_posix()))
    found.sort(key=lambda item: item[1])
    return found


def module_name_for(rel_posix, used):
    parts = []
    for part in rel_posix[:-3].split('/'):
        cleaned = re.sub(r'[^0-9A-Za-z_]', '_', part, flags=re.ASCII)
        if cleaned[:1].isdigit():
            cleaned = '_' + cleaned
        parts.append(cleaned)
    name = 'rt_' + '_'.join(parts)
    if name in used or not name.isidentifier():
        name += '_' + hashlib.sha256(rel_posix.encode('utf-8')).hexdigest()[:12]
    used.add(name)
    return name


def run_tests(tests_dir=None, stream=None):
    tests_dir = Path(tests_dir) if tests_dir is not None else ROOT / 'tests'
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    files = collect_test_files(tests_dir)
    problems = []
    zero = {'files': 0, 'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    if not files:
        print('ERROR: no test files found under tests/')
        return ['no test files'], zero
    used = set()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for full, rel in files:
        modname = module_name_for(rel, used)
        sys.modules.pop(modname, None)
        spec = importlib.util.spec_from_file_location(modname, full)
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            problems.append(f'{rel}: import failed: {type(exc).__name__}: {exc}')
            continue
        suite.addTests(loader.loadTestsFromModule(module))
    if suite.countTestCases() == 0:
        print('ERROR: zero test cases discovered')
        problems.append('zero test cases')
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    stats = {'files': len(files), 'tests': result.testsRun, 'failures': len(result.failures),
             'errors': len(result.errors), 'skipped': len(result.skipped)}
    if result.testsRun == 0 and 'zero test cases' not in problems:
        problems.append('zero tests ran')
    return problems, stats


def main(staged=False):
    print('== file checks ==')
    file_failures = check_files(staged)
    print('== tests ==')
    test_problems, stats = run_tests()
    ok = not file_failures and not test_problems
    print(f"RESULT: {'PASS' if ok else 'FAIL'} "
          f"(tests={stats['tests']} files={stats['files']} "
          f"failures={stats['failures']} errors={stats['errors']} skipped={stats['skipped']})")
    return 0 if ok else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true')
    raise SystemExit(main(parser.parse_args().staged))
