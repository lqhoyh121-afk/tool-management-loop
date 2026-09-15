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

from .codec import _put_identity
from .errors import UnsupportedShapeError, UnknownResultError
from contracts.model import Identity, text


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
            if command == 'stage.query':
                return self._stage_query(arguments)
            argv = self._argv(command, arguments)
            payload = self._run(argv)
        except subprocess.TimeoutExpired:
            return None
        except OSError:
            return None
        except UnknownResultError:
            return None
        if command in ('form.create', 'todo.create') and payload is not None:
            self._remember_stage(command, arguments, payload)
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
                    '--title', f"{arguments['action']}:{arguments['operation_id']}",
                    '--executors', arguments['actor'],
                    '--yes'] + common
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
        return {
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
            fields.return_container: '',
            fields.return_id: '',
        }

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
            return
        store = self._load_stages()
        store['by_operation'][arguments['operation_id']] = meta
        store['by_task'][meta['resource_id']] = meta
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
        detail = result.get('todoDetailModel') or {}
        executors = detail.get('executorIds') or []
        internal_id = executors[0] if executors else arguments.get('actor')
        return {
            'kind': 'todo',
            'resource_id': resource_id,
            'container': self.todo_container,
            'creation_evidence': f'todo.task.create:{resource_id}',
            'readback_evidence': f'todo.task.get:{resource_id}',
            'action': arguments['action'],
            'operation_id': arguments['operation_id'],
            'loan_container': arguments['loan_container'],
            'loan_id': arguments['loan_id'],
            'config_version': arguments['config_version'],
            'contact': arguments['actor'],
            'internal_id': internal_id,
        }

    def _stage_query(self, arguments):
        store = self._load_stages()
        if 'operation_id' in arguments:
            meta = store['by_operation'].get(arguments['operation_id'])
        else:
            meta = store['by_task'].get(arguments.get('task_id'))
        if meta is None:
            return ok_envelope(result={})
        return ok_envelope(result=dict(meta))
