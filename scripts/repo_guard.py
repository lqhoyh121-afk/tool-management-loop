"""Conservative tracked-file checks, not a complete secret scanner."""
from pathlib import Path
import argparse
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BANNED_PARTS = {'node_modules', '.venv', '__pycache__', 'deliverables', '.cache', 'logs',
                '.dev-flow', 'local-private', 'runtime'}
BANNED_SUFFIXES = ('.bak', '.pyc', '.log', '.tmp', '.swp')
HANDOVER_MARKS = ('handover', 'receipt', '交接', '回执')
SECRET_NAME_PARTS = {'key', 'token', 'secret', 'password', 'passwd', 'pwd', 'cred', 'creds',
                     'credential', 'credentials', 'auth', 'salt', 'apikey', 'privatekey'}
PLACEHOLDER_MARKS = ('xxx', '<', '>', '${', '{{', 'example', 'sample', 'placeholder',
                     'dummy', 'synthetic', 'fake', 'changeme', 'demo', 'todo', 'your_')
ASSIGNMENT = re.compile(
    r"(?im)^[\t ]*(?:export[\t ]+)?([A-Za-z_][A-Za-z0-9_-]*)[\t ]*[:=][\t ]*"
    r"(['\"])([^'\"\r\n]{8,})\2")


def _is_secret_name(name):
    return any(part.lower() in SECRET_NAME_PARTS for part in re.split(r'[_-]+', name))


def _is_placeholder(value):
    lowered = value.lower()
    return any(mark in lowered for mark in PLACEHOLDER_MARKS)


def _plaintext_assignments(text):
    found = []
    for match in ASSIGNMENT.finditer(text):
        name, value = match.group(1), match.group(3)
        if not _is_secret_name(name):
            continue
        if not value.isascii() or not all(ch.isprintable() for ch in value):
            continue
        if _is_placeholder(value):
            continue
        found.append('plaintext secret assignment')
    return found


def inspect(path, data):
    p = Path(path)
    issues = []
    if any(x in BANNED_PARTS for x in p.parts) or p.name.endswith(BANNED_SUFFIXES):
        issues.append('private/runtime artifact')
    if any(mark in p.name.lower() for mark in HANDOVER_MARKS[:2]) or \
            any(mark in p.name for mark in HANDOVER_MARKS[2:]):
        issues.append('local handover/receipt artifact')
    if '.local.' in p.name or p.name.endswith('.local'):
        issues.append('local config override')
    if p.name.startswith('.env') and p.name != '.env.example':
        issues.append('environment file')
    if p.suffix.lower() in {'.docx', '.xlsx', '.xls', '.pdf', '.db', '.sqlite', '.zip'}:
        issues.append('binary/data artifact needs separate approval')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return issues + ['non-UTF8 file needs separate approval']
    patterns = {
        'personal absolute path': r'(?i)([a-z]:[\\/]Users[\\/][^\s]+|/home/[a-z0-9_-]+/|/Users/[a-z0-9_-]+/)',
        'credential-shaped value': r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)',
        'embedded signature': r'(?im)^\s*(?:署名|作者|author)\s*[:：]\s*\S+',
    }
    for label, pattern in patterns.items():
        if re.search(pattern, text):
            issues.append(label)
    issues.extend(_plaintext_assignments(text))
    return issues


def run(staged=False):
    args = ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'] if staged else ['git', 'ls-files', '-z']
    names = subprocess.check_output(args, cwd=ROOT).decode('utf-8').split('\0')
    paths = [p for p in names if p]
    if not paths:
        print('No files to check.' if staged else 'ERROR: no tracked files')
        return 0 if staged else 1
    failures = []
    for path in paths:
        if staged:
            data = subprocess.check_output(['git', 'show', ':' + path], cwd=ROOT)
        else:
            data = (ROOT / path).read_bytes()
        for problem in inspect(path, data):
            failures.append(f'{path}: {problem}')
    print('\n'.join(failures) if failures else f'PASS: checked {len(paths)} files')
    return 1 if failures else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true')
    raise SystemExit(run(parser.parse_args().staged))
