"""Real dws CLI transport. No credentials; login state stays in the CLI.

Callers inject the argv prefix (typically ``node`` + ``dws.js``). This module
never reads APPDATA, accounts, or personal paths. Writes use ``--records-file``
with a Windows native path and are not retried when the subprocess yields no
envelope.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import NamedTuple

from .cells import read_creator, read_single_select
from .codec import _put_identity
from .errors import DingTalkShapeError, UnsupportedShapeError, UnknownResultError
from contracts.model import Code, ContractError, Identity, require, text


# ``--size`` above one page (20) makes the live CLI page and merge by itself, so
# the merged reply carries no ``hasMore``/``nextToken``; a reply that still admits
# more pages is rejected instead of being read as "no match".
TODO_LIST_PAGE_SIZE = 100

# Live ``todo task list --help`` (same account as this transport):
#
#     --status string   true=已完成, false=未完成
#
# The *default* (flag omitted) is not documented, and the live account could not
# falsify it: measured 2026-09-18 with this transport's own login, ``--status
# true`` → 47 cards, no flag → 47 cards, ``--status false`` → 0 cards, i.e.
# "default = all" and "default = completed only" look identical while nothing is
# open. Recovery therefore never relies on it: both predicates are asked for in
# separate calls and merged by ``taskId``, because the todo may have been ticked
# complete between create and readback.
TODO_LIST_STATUSES = (False, True)

# ``todo task list --help`` scope (verbatim): 「只返回当前登录用户作为执行者
# (executor) 的待办… 自己创建但交给他人执行的待办不在返回范围内」. Stage todos are
# created with ``--executors <业务当事人>``, which need not be the login account, so
# a title scan proves existence but a 0-hit scan only proves absence *inside the
# login user's executor scope*.
RECOVERY_MATCHED = 'matched'
RECOVERY_ABSENT = 'absent'
RECOVERY_AMBIGUOUS = 'ambiguous'
RECOVERY_UNREADABLE = 'unreadable'

# What a pending stage record says about its own recovery (see
# ``DwsTransport._annotate_recovery``): ``not_built`` is only ever written when
# the scan really covered this actor's todo space; everything else needs a human,
# because "never created" and "cannot see it" are the same 0 hits.
RECOVERY_NOT_BUILT = 'not_built'
RECOVERY_NOT_ATTEMPTED = 'not_attempted'
RECOVERY_NEEDS_MANUAL = 'needs_manual_confirmation'

SCOPE_SELF = 'self'
SCOPE_OTHER = 'other'
SCOPE_UNKNOWN = 'unknown'

REASON_EXECUTOR_SCOPE = 'executor_scope'
REASON_TITLE_AMBIGUOUS = 'title_ambiguous'
REASON_LIST_UNREADABLE = 'todo_list_unreadable'

_UNSET = object()


class _TitleScan(NamedTuple):
    """Result of scanning the todo space for one stage title."""

    outcome: str
    task_id: str | None = None
    matches: int = 0


def split_container(container_id):
    """Ledger container_id is baseId/tableId. Other shapes are unobserved."""
    if not isinstance(container_id, str) or container_id.count('/') != 1:
        raise UnsupportedShapeError('container_id 须为 baseId/tableId')
    base_id, table_id = container_id.split('/', 1)
    if not base_id or not table_id:
        raise UnsupportedShapeError('container_id 须为 baseId/tableId')
    return base_id, table_id


def _query_records(payload):
    """Rows from ``record query --all``; odd shapes and truncation fail closed.

    Live observation: an empty result set comes back as ``records: null``
    (present key, null value), not ``[]``. A *missing* key stays an error —
    that is a shape change, not an empty set.
    """
    if not isinstance(payload, dict):
        raise UnsupportedShapeError('record query 未返回对象报文')
    if payload.get('hasMore'):
        raise UnsupportedShapeError('record query 分页未拉完，拒绝按不完整结果匹配')
    if 'records' not in payload:
        raise UnsupportedShapeError('record query 报文缺少 records 键')
    records = payload['records']
    if records is None:
        records = []
    elif not isinstance(records, list):
        raise UnsupportedShapeError('record query 的 records 不是数组')
    rows = []
    for item in records:
        if not isinstance(item, dict):
            raise UnsupportedShapeError('record query 的记录不是对象')
        record_id = item.get('recordId')
        cells = item.get('cells')
        if not isinstance(record_id, str) or not record_id.strip():
            raise UnsupportedShapeError('record query 的记录缺少 recordId')
        if not isinstance(cells, dict):
            raise UnsupportedShapeError('record query 的记录缺少 cells')
        rows.append({'recordId': record_id, 'cells': cells})
    return rows


def windows_native_path(path):
    """``--records-file`` only accepts a Windows native absolute path.

    Forward slashes are legal on Windows; normalize with ``abspath`` first,
    then require a drive and backslash separators in the result.
    """
    abs_path = os.path.abspath(os.fspath(path))
    if os.name == 'nt':
        drive, tail = os.path.splitdrive(abs_path)
        if not drive or '/' in abs_path or not tail.startswith('\\'):
            raise UnsupportedShapeError('--records-file 只认 Windows 原生路径')
    return abs_path


def ok_envelope(**extra):
    payload = {'success': True, 'status': 'success', 'error': {}}
    payload.update(extra)
    return payload


def todo_internal_id(raw):
    """Accept todo-namespace internal IDs; reject contact-shaped strings."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return format(raw, 'd')
    if isinstance(raw, str):
        if not raw or '-' in raw:
            return None
        try:
            parsed = int(raw, 10)
        except ValueError:
            return None
        canonical = format(parsed, 'd')
        if raw != canonical:
            return None
        return canonical
    return None


def todo_title(arguments):
    """Title sent to ``todo task create``; doubles as the recovery key.

    The CLI falls back to ``action:operation_id`` when no title is supplied.
    Either way the value identifies exactly one stage, so a later readback may
    search the todo space for it.
    """
    title = arguments.get('title')
    if isinstance(title, str) and title:
        return title
    return f"{arguments['action']}:{arguments['operation_id']}"


def _todo_cards(payload):
    """``(subject, taskId)`` pairs from ``todo task list``; odd shapes fail closed.

    An unreadable list is **not** an empty list: the caller has to keep the
    stage unknown instead of concluding the artifact does not exist.
    """
    if not isinstance(payload, dict):
        raise UnsupportedShapeError('todo task list 未返回对象报文')
    result = payload.get('result')
    if not isinstance(result, dict):
        raise UnsupportedShapeError('todo task list 报文缺少 result')
    if result.get('hasMore') or result.get('nextToken'):
        raise UnsupportedShapeError('todo task list 分页未拉完，拒绝按不完整结果匹配')
    cards = result.get('todoCards')
    if cards is None:
        cards = []
    elif not isinstance(cards, list):
        raise UnsupportedShapeError('todo task list 的 todoCards 不是数组')
    pairs = []
    for card in cards:
        if not isinstance(card, dict):
            raise UnsupportedShapeError('todo task list 的卡片不是对象')
        subject = card.get('subject')
        task_id = card.get('taskId')
        if not isinstance(subject, str) or not subject:
            raise UnsupportedShapeError('todo task list 的卡片缺少 subject')
        if not isinstance(task_id, str) or not task_id:
            raise UnsupportedShapeError('todo task list 的卡片缺少 taskId')
        pairs.append((subject, task_id))
    return pairs


class DwsTransport:
    """Transport.exchange adapter for node-invoked dws.js (or a test double)."""

    def __init__(self, dws_cmd, fields, *, form_container, todo_container,
                 work_dir, entry_fields, timeout=30, extra_env=None):
        if not dws_cmd or not all(isinstance(part, str) and part for part in dws_cmd):
            raise UnsupportedShapeError('dws_cmd 必须由调用方注入，仓库不猜测安装路径')
        text(form_container)
        text(todo_container)
        self.dws_cmd = list(dws_cmd)
        self.fields = fields
        self.entry_fields = entry_fields
        self.form_container = form_container
        self.todo_container = todo_container
        self.work_dir = Path(work_dir)
        self.timeout = timeout
        self.extra_env = dict(extra_env or {})
        self._stage_path = self.work_dir / 'dws-stage-index.json'
        self._files_dir = self.work_dir / 'dws-records-file'
        self._login_user = _UNSET
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._files_dir.mkdir(parents=True, exist_ok=True)
        if not self._stage_path.exists():
            self._save_stages({'by_operation': {}, 'by_task': {}})

    def exchange(self, command, arguments):
        arguments = dict(arguments)
        try:
            if command == 'loan.query_borrowed':
                return self._loan_query_borrowed(arguments)
            if command == 'stage.query':
                return self._stage_query(arguments)
            argv = self._argv(command, arguments)
            payload = self._run(argv)
        except (subprocess.TimeoutExpired, OSError, UnknownResultError):
            payload = None
        if command in ('form.create', 'todo.create'):
            # No envelope, or an envelope we cannot turn into a durable stage
            # record: the create may still have landed. Remember the attempt so
            # stage.query can adopt the existing artifact instead of building a
            # second one. Never a blind retry.
            meta = None if payload is None else self._remember_stage(
                command, arguments, payload)
            if meta is None:
                self._remember_attempt(command, arguments)
        return payload

    def _argv(self, command, arguments):
        common = ['--format', 'json']
        if command == 'record.query':
            base_id, table_id = split_container(arguments['container_id'])
            argv = ['aitable', 'record', 'query',
                    '--base-id', base_id, '--table-id', table_id,
                    '--record-ids', arguments['resource_id']]
            if arguments.get('all'):
                argv.append('--all')
            return argv + common
        if command == 'record.update':
            base_id, table_id = split_container(arguments['container_id'])
            records = [{'recordId': arguments['resource_id'],
                        'cells': arguments['cells']}]
            return ['aitable', 'record', 'update',
                    '--base-id', base_id, '--table-id', table_id,
                    '--records-file', self._records_file(records),
                    '--yes'] + common
        if command == 'form.create':
            base_id, table_id = split_container(self.form_container)
            records = [{'cells': self._form_cells(arguments)}]
            return ['aitable', 'record', 'create',
                    '--base-id', base_id, '--table-id', table_id,
                    '--records-file', self._records_file(records),
                    '--yes'] + common
        if command == 'todo.create':
            return ['todo', 'task', 'create',
                    '--title', todo_title(arguments),
                    '--executors', arguments['actor'],
                    '--yes'] + common
        if command == 'todo.list':
            argv = ['todo', 'task', 'list',
                    '--size', str(arguments['size'])]
            status = arguments.get('status')
            if status is not None:
                argv.extend(['--status', 'true' if status else 'false'])
            return argv + common
        if command == 'auth.status':
            # Read-only: which account the CLI is logged in as. Needed to tell
            # whether a 0-hit title scan proves anything (the todo list is scoped
            # to the login user's executor todos).
            return ['auth', 'status'] + common
        if command == 'todo.get':
            return ['todo', 'task', 'get',
                    '--task-id', arguments['task_id']] + common
        if command == 'chat.send':
            title = arguments.get('title')
            body = arguments.get('text')
            if not isinstance(title, str) or not title or not isinstance(body, str) or not body:
                raise UnsupportedShapeError('chat.send 需要 title 与 text')
            if arguments.get('user'):
                recipient = ['--user', arguments['user']]
            elif arguments.get('open_dingtalk_id'):
                recipient = ['--open-dingtalk-id', arguments['open_dingtalk_id']]
            else:
                raise UnsupportedShapeError('chat.send 需要 user 或 open_dingtalk_id')
            return ['chat', 'message', 'send'] + recipient + [
                '--title', title, '--text', body, '--yes'] + common
        raise UnsupportedShapeError(f'未映射的内部命令: {command}')

    def _form_cells(self, arguments):
        tenant = arguments['tenant_id']
        fields = self.entry_fields
        cells = {
            fields.loan_container: arguments['loan_container'],
            fields.loan_id: arguments['loan_id'],
            fields.config_version: arguments['config_version'],
            fields.quantity: str(arguments['quantity']),
            fields.physical_ids: json.dumps(list(arguments['physical_ids']),
                                            ensure_ascii=True),
            fields.borrower: _put_identity(Identity('contact', tenant, arguments['borrower'])),
            fields.approver: _put_identity(Identity('contact', tenant, arguments['approver'])),
            fields.manager: _put_identity(Identity('contact', tenant, arguments['manager'])),
            fields.action: arguments['action'],
            fields.operation_id: arguments['operation_id'],
        }
        if arguments.get('return_container') and arguments.get('return_id'):
            cells[fields.return_container] = arguments['return_container']
            cells[fields.return_id] = arguments['return_id']
        return cells

    def _records_file(self, records):
        path = self._files_dir / f'{uuid.uuid4().hex}.json'
        path.write_text(json.dumps(records, ensure_ascii=True), encoding='utf-8')
        return windows_native_path(path)

    def _run(self, argv):
        env = os.environ.copy()
        env.update(self.extra_env)
        completed = subprocess.run(
            self.dws_cmd + argv,
            capture_output=True, text=True, encoding='utf-8',
            timeout=self.timeout, check=False, env=env,
        )
        raw = (completed.stdout or '').strip() or (completed.stderr or '').strip()
        if not raw:
            raise UnknownResultError('dws 无输出')
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UnknownResultError('dws 输出不是 JSON') from exc
        if not isinstance(payload, dict):
            raise UnknownResultError('dws 输出顶层不是对象')
        return payload

    def _load_stages(self):
        return json.loads(self._stage_path.read_text(encoding='utf-8'))

    def _save_stages(self, store):
        tmp = self._stage_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(store, ensure_ascii=True), encoding='utf-8')
        tmp.replace(self._stage_path)

    def _remember_stage(self, command, arguments, payload):
        if command == 'form.create':
            meta = self._form_stage_meta(arguments, payload)
        else:
            meta = self._todo_stage_meta(arguments, payload)
        if meta is None:
            return None
        store = self._load_stages()
        store['by_operation'][arguments['operation_id']] = meta
        task_key = meta.get('resource_id') or meta.get('claimed_task_id')
        if isinstance(task_key, str) and task_key:
            store['by_task'][task_key] = meta
        self._save_stages(store)
        return meta

    def _remember_attempt(self, command, arguments):
        """Record a create that produced no usable receipt, keyed by its target.

        The write may have landed before the envelope was lost. Keeping the
        attempt (todo title included) is what lets ``stage.query`` adopt that
        already-created artifact by a unique title match later. Nothing here
        creates anything, and an indexed stage is never downgraded.
        """
        store = self._load_stages()
        operation_id = arguments.get('operation_id')
        known = store['by_operation'].get(operation_id)
        if isinstance(known, dict) and known.get('resource_id'):
            return
        if command == 'todo.create':
            scope, login_user = self._executor_scope(arguments['actor'])
            meta = {'kind': 'todo',
                    'pending': True,
                    'title': todo_title(arguments),
                    'container': self.todo_container,
                    'executor_contact': arguments['actor'],
                    # Whose todo list could hold this stage is part of the pending
                    # record from the start: a later 0-hit title scan may only be
                    # read as "never created" when the actor *is* the login user.
                    'login_user_id': login_user,
                    'executor_scope': scope,
                    'recovery_state': RECOVERY_NOT_ATTEMPTED}
        else:
            meta = {'kind': 'form',
                    'pending': True,
                    'container': self.form_container}
        meta.update({'action': arguments['action'],
                     'operation_id': operation_id,
                     'loan_container': arguments['loan_container'],
                     'loan_id': arguments['loan_id'],
                     'config_version': arguments['config_version'],
                     'contact': arguments['actor']})
        store['by_operation'][operation_id] = meta
        self._save_stages(store)

    def _form_stage_meta(self, arguments, payload):
        ids = (payload.get('data') or {}).get('newRecordIds') or []
        if not isinstance(ids, list) or not ids or not isinstance(ids[0], str) or not ids[0]:
            return None
        resource_id = ids[0]
        return {
            'kind': 'form',
            'resource_id': resource_id,
            'container': self.form_container,
            'creation_evidence': f'aitable.record.create:{resource_id}',
            'readback_evidence': f'aitable.record.query:{resource_id}',
            'action': arguments['action'],
            'operation_id': arguments['operation_id'],
            'loan_container': arguments['loan_container'],
            'loan_id': arguments['loan_id'],
            'config_version': arguments['config_version'],
            'contact': arguments['actor'],
        }

    def _todo_stage_meta(self, arguments, payload):
        result = payload.get('result') or {}
        resource_id = result.get('taskId')
        if not isinstance(resource_id, str) or not resource_id:
            return None
        detail = self._todo_detail_for_stage(resource_id)
        internal_id = (self._todo_internal_id_from_detail(detail)
                       if detail is not None else None)
        shared = {
            'kind': 'todo',
            'container': self.todo_container,
            'action': arguments['action'],
            'operation_id': arguments['operation_id'],
            'loan_container': arguments['loan_container'],
            'loan_id': arguments['loan_id'],
            'config_version': arguments['config_version'],
            'contact': arguments['actor'],
            'executor_contact': arguments['actor'],
            'title': todo_title(arguments),
        }
        if internal_id is not None:
            return {
                **shared,
                'resource_id': resource_id,
                'creation_evidence': f'todo.task.create:{resource_id}',
                'readback_evidence': f'todo.task.get:{resource_id}',
                'internal_id': internal_id,
            }
        # Create succeeded but readback is incomplete — keep the known task id.
        scope, login_user = self._executor_scope(arguments['actor'])
        return {**shared, 'pending': True, 'claimed_task_id': resource_id,
                'login_user_id': login_user, 'executor_scope': scope,
                'recovery_state': RECOVERY_NOT_ATTEMPTED}

    def _todo_internal_id_from_detail(self, detail):
        """Executor ID from a ``todo task get`` detail; unreadable is ``None``."""
        if not isinstance(detail, dict):
            return None
        executors = detail.get('executorIds') or []
        if not isinstance(executors, list) or not executors:
            return None
        return todo_internal_id(executors[0])

    def _todo_detail_for_stage(self, task_id):
        """Re-read todo after create; live create may not return todo internal executorIds."""
        try:
            payload = self._run(self._argv('todo.get', {'task_id': task_id}))
        except (UnknownResultError, UnsupportedShapeError, subprocess.TimeoutExpired, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        result = payload.get('result') or {}
        detail = result.get('todoDetailModel')
        return detail if isinstance(detail, dict) else None

    def _stage_query(self, arguments):
        store = self._load_stages()
        if 'operation_id' in arguments:
            meta = store['by_operation'].get(arguments['operation_id'])
        else:
            meta = store['by_task'].get(arguments.get('task_id'))
        if isinstance(meta, dict) and meta.get('pending'):
            # Adopt the artifact when readback is complete; otherwise surface the
            # pending record (including any claimed_task_id) for a later retry,
            # annotated with why it is still unresolved and whether it needs a
            # human (a 0-hit title scan is not "never created").
            recovered, meta = self._recover_pending(meta)
            if recovered is not None:
                meta = recovered
        if meta is None:
            return ok_envelope(result={})
        return ok_envelope(result=dict(meta))

    def _recover_pending(self, meta):
        """Adopt an already-created todo for a pending stage, else ``None``.

        Returns ``(recovered_or_None, meta)``: the meta carried back is the
        pending record as it should be surfaced, recovery annotations included.

        When ``claimed_task_id`` is known, ``todo task get`` is tried first — it
        is addressed by task id, not by executor scope, so the login-scoped list
        does not matter. Title matching only runs when no task id was ever
        recorded, and an inconclusive scan annotates the pending record instead of
        passing for "not built".
        """
        if meta.get('kind') != 'todo':
            return None, meta
        claimed = meta.get('claimed_task_id')
        if isinstance(claimed, str) and claimed:
            return self._finalize_todo_recovery(meta, claimed), meta
        title = meta.get('title')
        if not isinstance(title, str) or not title:
            return None, meta
        scan = self._todo_title_scan(title)
        if scan.outcome == RECOVERY_MATCHED:
            return self._finalize_todo_recovery(meta, scan.task_id), meta
        return None, self._annotate_recovery(meta, scan)

    def _finalize_todo_recovery(self, meta, task_id):
        detail = self._todo_detail_for_stage(task_id)
        internal_id = self._todo_internal_id_from_detail(detail)
        if internal_id is None:
            return None
        recovered = dict(meta)
        recovered.pop('pending', None)
        recovered.pop('claimed_task_id', None)
        recovered['resource_id'] = task_id
        recovered['creation_evidence'] = f'todo.task.create:{task_id}'
        recovered['readback_evidence'] = f'todo.task.get:{task_id}'
        recovered['internal_id'] = internal_id
        store = self._load_stages()
        store['by_operation'][recovered['operation_id']] = recovered
        store['by_task'][task_id] = recovered
        self._save_stages(store)
        return recovered

    def _todo_list_pairs(self):
        """``(subject, taskId)`` of *both* completion statuses, merged.

        The unflagged live default is ambiguous (see ``TODO_LIST_STATUSES``), so
        recovery asks for ``--status false`` and ``--status true`` explicitly — a
        todo the human completed between create and readback is only in the
        second one. A failed or unreadable *either* call raises: half the todo
        space is not a scan, and the caller must not read it as "no match".
        """
        merged = {}
        for done in TODO_LIST_STATUSES:
            payload = self._run(self._argv(
                'todo.list', {'size': TODO_LIST_PAGE_SIZE, 'status': done}))
            for subject, task_id in _todo_cards(payload):
                merged.setdefault(task_id, subject)
        return [(subject, task_id) for task_id, subject in merged.items()]

    def _todo_title_scan(self, title):
        """Scan **both** statuses for ``title``; 0 hits is not proof of absence.

        Three outcomes stay apart on purpose: ``matched`` (exactly one card),
        ``absent`` (no card in this scan), ``ambiguous`` (2+ cards share the
        title) and ``unreadable`` (the list itself could not be read). The live
        list only covers the login user's own executor todos, so ``absent`` is
        only conclusive together with the actor/login relation — the caller
        decides that in ``_annotate_recovery``.
        """
        try:
            cards = self._todo_list_pairs()
        except (UnknownResultError, UnsupportedShapeError,
                subprocess.TimeoutExpired, OSError):
            return _TitleScan(RECOVERY_UNREADABLE)
        matches = [task_id for subject, task_id in cards if subject == title]
        if len(matches) == 1:
            return _TitleScan(RECOVERY_MATCHED, matches[0], 1)
        if not matches:
            return _TitleScan(RECOVERY_ABSENT)
        return _TitleScan(RECOVERY_AMBIGUOUS, None, len(matches))

    def _todo_task_id_by_title(self, title):
        """Task id of the *only* todo titled ``title``, else ``None``.

        ``None`` covers absent, ambiguous **and** unreadable — callers that have
        to tell those apart use ``_todo_title_scan``.
        """
        scan = self._todo_title_scan(title)
        return scan.task_id if scan.outcome == RECOVERY_MATCHED else None

    def _annotate_recovery(self, meta, scan):
        """Write down why a pending stage is unresolved, and who must confirm it.

        ``not_built`` is claimed **only** when the scan really covered this
        actor's todo space: both completion statuses read back *and* the stage
        actor is the dws login account, whose executor todos the live list
        returns. Everywhere else — 0 hits while the actor is someone else (or the
        login is unobservable), 2+ cards sharing the title, an unreadable list —
        the record says ``needs_manual_confirmation``, because "the todo was never
        created" and "the todo is invisible to this login" produce the same 0
        hits. Annotating here is what keeps a stuck stage from looking like a
        plain never-created one forever.
        """
        scope, login_user = self._executor_scope(meta.get('executor_contact'))
        if scan.outcome == RECOVERY_ABSENT and scope == SCOPE_SELF:
            state, reason = RECOVERY_NOT_BUILT, None
        elif scan.outcome == RECOVERY_ABSENT:
            state, reason = RECOVERY_NEEDS_MANUAL, REASON_EXECUTOR_SCOPE
        elif scan.outcome == RECOVERY_AMBIGUOUS:
            state, reason = RECOVERY_NEEDS_MANUAL, REASON_TITLE_AMBIGUOUS
        else:
            state, reason = RECOVERY_NEEDS_MANUAL, REASON_LIST_UNREADABLE
        annotated = dict(meta)
        annotated.update({
            'recovery_state': state,
            'needs_manual_confirmation': state != RECOVERY_NOT_BUILT,
            'recovery_reason': reason,
            'recovery_attempts': int(meta.get('recovery_attempts') or 0) + 1,
            'recovery_matches': scan.matches,
            'recovery_statuses': ['false', 'true'],
            'login_user_id': login_user,
            'executor_scope': scope,
        })
        operation_id = meta.get('operation_id')
        if isinstance(operation_id, str) and operation_id:
            store = self._load_stages()
            store['by_operation'][operation_id] = annotated
            self._save_stages(store)
        return annotated

    def _executor_scope(self, actor):
        """``(relation, login_user_id)`` between a stage actor and the login user.

        ``self`` means the todo list covers this actor's todos, so a 0-hit scan
        means something; ``other`` means the todo exists but is out of the list's
        reach; ``unknown`` means the login account could not be observed (never a
        guess, never treated as proof).
        """
        login_user = self._login_user_id()
        if login_user is None:
            return SCOPE_UNKNOWN, None
        if isinstance(actor, str) and actor and actor == login_user:
            return SCOPE_SELF, login_user
        return SCOPE_OTHER, login_user

    def _login_user_id(self):
        """Contact id the CLI is logged in as, or ``None`` when unobservable.

        A successful observation is cached for this transport's lifetime; an
        unreadable one is **not**, so a transient ``auth status`` failure cannot
        blind the whole process into treating every scan as scope-unknown.
        """
        if self._login_user is _UNSET:
            observed = self._read_login_user()
            if observed is not None:
                self._login_user = observed
            return observed
        return self._login_user

    def _read_login_user(self):
        try:
            payload = self._run(self._argv('auth.status', {}))
        except (UnknownResultError, UnsupportedShapeError,
                subprocess.TimeoutExpired, OSError):
            return None
        if not isinstance(payload, dict) or payload.get('authenticated') is not True:
            return None
        user_id = payload.get('user_id')
        if not isinstance(user_id, str) or not user_id.strip():
            return None
        return user_id

    def _loan_query_borrowed(self, arguments):
        """Borrowed loans of one borrower, via ``aitable record query --all``.

        The live ``--all`` shape is ``{hasMore, pages, records}``; it is **not**
        the ``record list`` envelope. Anything we cannot read (missing cells,
        truncated pages) fails closed with ``EVIDENCE``: under-counting would
        turn "more than one open loan" into a confident single match.
        """
        base_id, table_id = split_container(arguments.get('loan_container'))
        tenant_id = str(arguments.get('tenant_id', ''))
        borrower = str(arguments.get('borrower', ''))
        require(bool(tenant_id.strip()) and bool(borrower.strip()), Code.EVIDENCE)
        state_field = self.fields.state
        borrower_field = self.fields.borrower
        filters = {'operator': 'and',
                   'operands': [{'operator': 'eq', 'operands': [state_field, 'borrowed']}]}
        argv = ['aitable', 'record', 'query',
                '--base-id', base_id, '--table-id', table_id,
                '--filters', json.dumps(filters, ensure_ascii=True),
                '--field-ids', f'{state_field},{borrower_field}',
                '--all', '--format', 'json']
        payload = self._run(argv)
        try:
            rows = _query_records(payload)
            loan_ids = []
            for row in rows:
                cells = row['cells']
                state = read_single_select(cells, state_field).name
                who = read_creator(cells, borrower_field)
                if state == 'borrowed' and who.value == borrower:
                    loan_ids.append(row['recordId'])
        except (DingTalkShapeError, KeyError, TypeError) as exc:
            raise ContractError(Code.EVIDENCE) from exc
        return ok_envelope(result={'loan_ids': sorted(loan_ids)})


def pending_stage_report(work_dir):
    """Read-only ops view of the unresolved stage creates in a run directory.

    The stage index is the only place that knows about a create whose reply was
    lost, so this is the exit a human needs: one row per pending stage, with the
    action, the title (or the claimed task id when the reply did arrive but the
    post-create read did not) and what the last recovery attempt concluded.

    ``needs_manual_confirmation`` means the stage was **not** proven missing: the
    title scan either ran outside the login user's executor scope or could not
    read the list at all, so a human has to look the todo up in DingTalk before
    the driver is allowed to create a second one. Only ``not_built`` (scan over
    both completion statuses, actor = login account, still 0 hits) is a stage
    that is safe to build.
    """
    path = Path(work_dir) / 'dws-stage-index.json'
    if not path.exists():
        return ()
    store = json.loads(path.read_text(encoding='utf-8'))
    by_operation = store.get('by_operation') or {}
    if not isinstance(by_operation, dict):
        raise UnsupportedShapeError('dws-stage-index.json 的 by_operation 不是对象')
    rows = []
    for operation_id, meta in sorted(by_operation.items()):
        if not isinstance(meta, dict) or not meta.get('pending'):
            continue
        rows.append({
            'operation_id': operation_id,
            'kind': meta.get('kind'),
            'action': meta.get('action'),
            'loan_id': meta.get('loan_id'),
            'title': meta.get('title'),
            'claimed_task_id': meta.get('claimed_task_id'),
            'executor_contact': meta.get('executor_contact'),
            'executor_scope': meta.get('executor_scope', SCOPE_UNKNOWN),
            'login_user_id': meta.get('login_user_id'),
            'recovery_state': meta.get('recovery_state', RECOVERY_NOT_ATTEMPTED),
            'needs_manual_confirmation': bool(meta.get('needs_manual_confirmation')),
            'recovery_reason': meta.get('recovery_reason'),
            'recovery_attempts': int(meta.get('recovery_attempts') or 0),
        })
    return tuple(rows)
