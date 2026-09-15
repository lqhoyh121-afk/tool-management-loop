"""Runtime and lock locations from config or well-known roots, never a developer path."""
import os
from pathlib import Path


def repo_root():
    return Path(__file__).resolve().parents[1]


def runtime_dir(explicit=None):
    if explicit:
        return Path(explicit)
    override = os.environ.get('TOOL_LOOP_RUNTIME')
    if override:
        return Path(override)
    return repo_root() / 'runtime'


def lock_root(explicit=None):
    """Machine-wide lock root. Independent of the runtime working directory.

    Override with TOOL_LOOP_LOCK_ROOT in tests. Default is ProgramData so two
    checkouts still collide on the same ledger scope.
    """
    if explicit:
        return Path(explicit)
    override = os.environ.get('TOOL_LOOP_LOCK_ROOT')
    if override:
        return Path(override)
    programdata = os.environ.get('PROGRAMDATA')
    if programdata:
        return Path(programdata) / 'tool-management-loop' / 'locks'
    return Path(os.environ.get('TEMP', '.')) / 'tool-management-loop-locks'
