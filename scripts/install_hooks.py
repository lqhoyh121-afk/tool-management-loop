"""Install a repository-local hook without changing global Git config."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
subprocess.run(['git', 'rev-parse', '--git-dir'], cwd=root, check=True, capture_output=True)
hook = root / '.githooks' / 'pre-commit'
if not hook.is_file():
    raise SystemExit('Missing .githooks/pre-commit; nothing to install.')
old = subprocess.run(['git', 'config', '--local', '--get', 'core.hooksPath'], cwd=root, capture_output=True, text=True)
if old.returncode == 0 and old.stdout.strip() != '.githooks':
    raise SystemExit('Existing hooksPath differs; inspect it before replacing.')
subprocess.run(['git', 'config', '--local', 'core.hooksPath', '.githooks'], cwd=root, check=True)
check = subprocess.run(['git', 'config', '--local', '--get', 'core.hooksPath'], cwd=root, capture_output=True, text=True, check=True)
print(f'Installed repository-local hooks (core.hooksPath={check.stdout.strip()}).')
print('Pre-commit runs: python scripts/repo_checks.py --staged. Business deployment is not enabled.')
