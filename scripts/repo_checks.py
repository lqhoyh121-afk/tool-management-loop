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


def declares_load_tests(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(node, ast.FunctionDef) and node.name == 'load_tests'
               for node in tree.body)


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
    resolved = {str(full.resolve()): (full, rel) for full, rel in files}
    used = set()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    def load_module(full, rel):
        modname = module_name_for(rel, used)
        sys.modules.pop(modname, None)
        spec = importlib.util.spec_from_file_location(modname, full)
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)
        return loader.loadTestsFromModule(module)

    def iter_cases(tests):
        stack = list(tests)
        while stack:
            item = stack.pop()
            if isinstance(item, unittest.TestSuite):
                stack.extend(item)
            else:
                yield item

    def case_key(case):
        module = sys.modules.get(type(case).__module__)
        loaded = getattr(module, '__file__', None)
        if not loaded:
            return None
        return (str(Path(loaded).resolve()), type(case).__qualname__, case._testMethodName)

    # Phase 1: adapter files (module-level load_tests, detected statically)
    # execute first. Ownership of an imported file is decided by the cases the
    # adapter actually returns, never by the import itself.
    adapter_runs = []
    imported = {}
    plain = []
    for full, rel in files:
        try:
            source = full.read_bytes()
        except OSError as exc:
            problems.append(f'{rel}: load failed: {type(exc).__name__}: {exc}')
            continue
        if not declares_load_tests(source):
            plain.append((full, rel))
            continue
        before = set(sys.modules)
        try:
            loaded = load_module(full, rel)
        except Exception as exc:
            problems.append(f'{rel}: load failed: {type(exc).__name__}: {exc}')
            continue
        for name in set(sys.modules) - before:
            mod_file = getattr(sys.modules[name], '__file__', None)
            if mod_file:
                key = str(Path(mod_file).resolve())
                if key in resolved and key not in imported:
                    imported[key] = name
        for item in iter_cases(loaded):
            if type(item).__name__ == '_FailedTest':
                problems.append(f'{rel}: load failed: load_tests raised during suite construction')
                break
        adapter_runs.append((rel, loaded))

    contributed = {}
    for _rel, loaded in adapter_runs:
        for case in iter_cases(loaded):
            key = case_key(case)
            if key:
                contributed.setdefault(key[0], set()).add(key)
        suite.addTests(loaded)

    # Phase 2: direct discovery. Files fully covered by an adapter's returned
    # suite are skipped; files imported but never collected still run; files
    # only partially collected fail closed instead of guessing.
    for full, rel in plain:
        fkey = str(full.resolve())
        if fkey not in imported:
            try:
                suite.addTests(load_module(full, rel))
            except Exception as exc:
                problems.append(f'{rel}: load failed: {type(exc).__name__}: {exc}')
            continue
        module = sys.modules.get(imported[fkey])
        if module is None:
            problems.append(f'{rel}: adapter imported this file but left no module; failing closed')
            continue
        try:
            expected = unittest.TestLoader().loadTestsFromModule(module)
            expected_keys = {case_key(case) for case in iter_cases(expected)}
        except Exception as exc:
            problems.append(f'{rel}: cannot verify adapter coverage: {type(exc).__name__}: {exc}')
            continue
        collected = contributed.get(fkey, set()) & expected_keys
        if not collected:
            suite.addTests(expected)
        elif expected_keys - collected:
            problems.append(f'{rel}: adapter collected only {len(collected)} of '
                            f'{len(expected_keys)} cases; failing closed')

    # Global duplicate guard: no source case may execute twice.
    seen = {}
    for case in iter_cases(suite):
        if type(case).__name__ == '_FailedTest':
            continue
        key = case_key(case)
        if key:
            seen[key] = seen.get(key, 0) + 1
    for (fpath, qualname, _method), count in seen.items():
        if count > 1:
            problems.append(f'duplicate test case execution: {qualname} in {fpath} ran {count} times')

    if suite.countTestCases() == 0:
        print('ERROR: zero test cases discovered')
        problems.append('zero test cases')
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    stats = {'files': len(files), 'tests': result.testsRun, 'failures': len(result.failures),
             'errors': len(result.errors), 'skipped': len(result.skipped)}
    if result.testsRun == 0 and 'zero test cases' not in problems:
        problems.append('zero tests ran')
    if not result.wasSuccessful():
        problems.append(f'{len(result.failures)} test failure(s), {len(result.errors)} test error(s)')
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
