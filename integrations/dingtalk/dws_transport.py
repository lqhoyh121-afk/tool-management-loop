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

from .cells import read_creator, read_single_select
from .codec import _put_identity
from .envelope import query_rows
from .errors import DingTalkShapeError, UnsupportedShapeError, UnknownResultError
from contracts.model import Code, ContractError, Identity, require, text


# One page is enough to prove a stage title is absent; a reply that admits more
# pages is rejected instead of being read as "no match".
TODO_LIST_PAGE_SIZE = 100


def split_container(container_id):
    """Ledger container_id is baseId/tableId. Other shapes are unobserved."""
    if not isinstance(container_id, str) or container_id.count('/') != 1:
        raise UnsupportedShapeError('container_id 须为 baseId/tableId')
    base_id, table_id = container_id.split('/', 1)
    if not base_id or not table_id:
        raise UnsupportedShapeError('container_id 须为 baseId/tableId')
    return base_id, table_id


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
        if command == 'application.list':
            # 申请收集表结果表全量列出：只回行 id 与单元格，不在传输层过滤。
            base_id, table_id = split_container(arguments['container_id'])
            return ['aitable', 'record', 'query',
                    '--base-id', base_id, '--table-id', table_id,
                    '--all'] + common
        if command == 'loan.find_application':
            # 按台账「申请证据」格精确匹配链路键；只取该列，避免把整行读进来。
            base_id, table_id = split_container(arguments['container_id'])
            marker_field = self.fields.application_evidence
            filters = {'operator': 'and', 'operands': [
                {'operator': 'eq', 'operands': [marker_field, arguments['marker']]}]}
            return ['aitable', 'record', 'query',
                    '--base-id', base_id, '--table-id', table_id,
                    '--filters', json.dumps(filters, ensure_ascii=True),
                    '--field-ids', marker_field,
                    '--all'] + common
        if command == 'loan.create':
            base_id, table_id = split_container(arguments['container_id'])
            records = [{'cells': arguments['cells']}]
            return ['aitable', 'record', 'create',
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
            meta = {'kind': 'todo',
                    'pending': True,
                    'title': todo_title(arguments),
                    'container': self.todo_container,
                    'executor_contact': arguments['actor']}
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
        return {**shared, 'pending': True, 'claimed_task_id': resource_id}

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
            # pending record (including any claimed_task_id) for a later retry.
            recovered = self._recover_pending(meta)
            if recovered is not None:
                meta = recovered
        if meta is None:
            return ok_envelope(result={})
        return ok_envelope(result=dict(meta))

    def _recover_pending(self, meta):
        """Adopt an already-created todo for a pending stage, else ``None``.

        When ``claimed_task_id`` is known, ``todo task get`` is tried first — the
        login-scoped list may not show todos whose executor differs from the CLI
        account. Title matching is only used when no task id was ever recorded.
        """
        if meta.get('kind') != 'todo':
            return None
        claimed = meta.get('claimed_task_id')
        if isinstance(claimed, str) and claimed:
            return self._finalize_todo_recovery(meta, claimed)
        title = meta.get('title')
        if not isinstance(title, str) or not title:
            return None
        task_id = self._todo_task_id_by_title(title)
        if task_id is None:
            return None
        return self._finalize_todo_recovery(meta, task_id)

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
        """Merge incomplete and complete todos; default list status is ambiguous."""
        merged = {}
        for done in (False, True):
            payload = self._run(self._argv(
                'todo.list', {'size': TODO_LIST_PAGE_SIZE, 'status': done}))
            for subject, task_id in _todo_cards(payload):
                merged.setdefault(task_id, subject)
        return [(subject, task_id) for task_id, subject in merged.items()]

    def _todo_task_id_by_title(self, title):
        """Task id of the *only* todo titled ``title``, else ``None``."""
        try:
            cards = self._todo_list_pairs()
        except (UnknownResultError, UnsupportedShapeError,
                subprocess.TimeoutExpired, OSError):
            return None
        matches = [task_id for subject, task_id in cards if subject == title]
        if len(matches) != 1:
            return None
        return matches[0]

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
            rows = query_rows(payload)
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
