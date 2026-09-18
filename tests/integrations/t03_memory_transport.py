"""Injected in-memory DingTalk transport. Test-only, no platform calls.

Module name is unique so T09 file-based discovery does not collide with other
helpers. Commands are adapter-internal names, not a claim of real dws verbs.
"""
from copy import deepcopy
import json

from t03_live_cells import declared_kinds, live_cells

from contracts.model import Identity, Resource, State

from integrations.dingtalk.codec import _put_identity, decode_loan
from integrations.dingtalk.transport import Transport


def ok_envelope(**extra):
    payload = {'success': True, 'status': 'success', 'error': {}}
    payload.update(extra)
    return payload


def error_envelope(code, message='SYNTHETIC-denied'):
    return {
        'success': True,
        'status': 'error',
        'error': {'code': code, 'message': message},
    }


def todo_ok_envelope(**extra):
    payload = {
        'success': True,
        'errorCode': None,
        'errorMsg': None,
        'arguments': [],
    }
    payload.update(extra)
    return payload


def todo_error_envelope(code, message='SYNTHETIC-denied'):
    return {
        'success': False,
        'errorCode': code,
        'errorMsg': message,
        'arguments': [],
        'result': None,
    }


def _synthetic_option_write(cells):
    for value in cells.values():
        if not isinstance(value, dict):
            continue
        option_id = value.get('id')
        if isinstance(option_id, str) and option_id.startswith('SYNTHETIC-opt-'):
            return True
    return False


class MemoryTransport(Transport):
    """Records, forms and todos in process memory.

    Todo completion time is carried only in ``finishTime`` (milliseconds), matching
    live ``todo task get``; tests must not fabricate ``result.occurredAt``.
    """

    def __init__(self, fields, entry_fields, form_container='synthetic-forms',
                 todo_container='synthetic-todos', apply_container='synthetic-apply-forms',
                 apply_fields=None):
        self.fields = fields
        self.entry_fields = entry_fields
        self.kinds = declared_kinds(fields, entry_fields, apply_fields)
        self.form_container = form_container
        self.apply_container = apply_container
        self.todo_container = todo_container
        self.records = {}
        self.stages = {}
        self.todos = {}
        self.by_task = {}
        self.calls = []
        self.fail_codes = {}
        self.drop_once = []
        self.drop_commands = set()
        self.drop_after_updates = None
        self.update_count = 0
        self._forms = 0
        self._loans = 0
        self._todos = 0
        self._activities = 0
        self._next_internal = 9000000100
        self.contact_to_internal = {}
        # 标题显示名（issue #76）：空表示查不到，调用方退回 id；code 非 None 时按错误回。
        self.title_names = {}
        self.title_names_code = None

    def seed_record(self, ref, cells):
        self.records[(ref.tenant_id, ref.container_id, ref.resource_id)] = dict(cells)

    def exchange(self, command, arguments):
        arguments = dict(arguments)
        self.calls.append((command, arguments))
        if command in self.drop_commands or command in self.drop_once:
            if command in self.drop_once:
                self.drop_once.remove(command)
            return None
        if command in self.fail_codes:
            code = self.fail_codes[command]
            if command in ('todo.create', 'todo.get'):
                return todo_error_envelope(code)
            return error_envelope(code)
        handler = {
            'record.query': self._record_query,
            'record.update': self._record_update,
            'form.create': self._form_create,
            'todo.create': self._todo_create,
            'todo.get': self._todo_get,
            'stage.query': self._stage_query,
            'loan.query_borrowed': self._loan_query_borrowed,
            'row.list': self._row_list,
            'loan.create': self._loan_create,
            'loan.find_application': self._loan_find_application,
            'title.names': self._title_names,
        }.get(command)
        if handler is None:
            return error_envelope('UNSUPPORTED_COMMAND')
        return handler(arguments)

    def complete_form(self, form_id, decision, occurred_at, **extra):
        key = ('synthetic-org', self.form_container, form_id)
        cells = self.records[key]
        fields = self.entry_fields
        cells[fields.decision] = {
            'id': f'SYNTHETIC-rand-{decision}', 'name': decision,
        }
        cells[fields.occurred_at] = occurred_at
        if 'return_container' in extra:
            cells[fields.return_container] = extra['return_container']
            cells[fields.return_id] = extra['return_id']
        if 'quantity' in extra:
            cells[fields.quantity] = str(extra['quantity'])
        if 'physical_ids' in extra:
            cells[fields.physical_ids] = json.dumps(
                list(extra['physical_ids']), ensure_ascii=True)

    def complete_todo(self, task_id, occurred_at, creator_id=None):
        todo = self.todos[task_id]
        internal = creator_id if creator_id is not None else todo['internal_id']
        self._activities += 1
        activity = f'SYNTHETIC-activity-{self._activities:04d}'
        if isinstance(occurred_at, str):
            from datetime import datetime
            occurred_at = datetime.fromisoformat(occurred_at)
        todo['detail']['isDone'] = True
        todo['detail']['finishTime'] = int(occurred_at.timestamp() * 1000)
        todo['detail']['activities'] = [
            {'activityId': activity + '-self', 'action': 'task.self.done', 'creatorId': internal},
            {'activityId': activity + '-done', 'action': 'task.done', 'creatorId': internal},
        ]

    def writes_of(self, command):
        return [item for item in self.calls if item[0] == command]

    def _key(self, arguments):
        return (arguments['tenant_id'], arguments['container_id'], arguments['resource_id'])

    def _internal_for(self, contact):
        if contact not in self.contact_to_internal:
            self.contact_to_internal[contact] = self._next_internal
            self._next_internal += 1
        return self.contact_to_internal[contact]

    def _record_query(self, arguments):
        key = self._key(arguments)
        if key not in self.records:
            return ok_envelope(data={'records': [], 'hasMore': False})
        record = {
            'recordId': arguments['resource_id'],
            'cells': self._live_cells(self.records[key]),
        }
        return ok_envelope(data={'records': [record], 'hasMore': False})

    def _live_cells(self, cells):
        """Read side: stored write payload re-shaped as a live read (shared policy)."""
        return live_cells(cells, self.kinds)

    def _record_update(self, arguments):
        if self.drop_after_updates is not None and self.update_count >= self.drop_after_updates:
            return None
        key = self._key(arguments)
        if key not in self.records:
            return error_envelope('RECORD_NOT_FOUND')
        cells = arguments['cells']
        if _synthetic_option_write(cells):
            return error_envelope('SELECT_OPTION_NOT_FOUND')
        self.records[key] = dict(cells)
        self.update_count += 1
        return ok_envelope(result={'recordId': arguments['resource_id']})

    def _form_create(self, arguments):
        self._forms += 1
        form_id = f'SYNTHETIC-form-{self._forms:04d}'
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
        self.seed_record(Resource('form', tenant, self.form_container, form_id), cells)
        meta = {
            'kind': 'form',
            'resource_id': form_id,
            'container': self.form_container,
            'creation_evidence': f'form.create:{form_id}',
            'readback_evidence': f'form.get:{form_id}',
            'action': arguments['action'],
            'operation_id': arguments['operation_id'],
            'loan_container': arguments['loan_container'],
            'loan_id': arguments['loan_id'],
            'config_version': arguments['config_version'],
            'contact': arguments['actor'],
        }
        self.stages[arguments['operation_id']] = meta
        return ok_envelope(result={'formId': form_id})

    def _todo_create(self, arguments):
        self._todos += 1
        task_id = f'SYNTHETIC-todo-{self._todos:04d}'
        internal = self._internal_for(arguments['actor'])
        detail = {
            'taskId': task_id,
            'isDone': False,
            'finishTime': 0,
            'executorIds': [internal],
            'activities': [],
        }
        self.todos[task_id] = {
            'detail': detail,
            'internal_id': internal,
            'operation_id': arguments['operation_id'],
        }
        meta = {
            'kind': 'todo',
            'resource_id': task_id,
            'container': self.todo_container,
            'creation_evidence': f'todo.create:{task_id}',
            'readback_evidence': f'todo.get:{task_id}',
            'internal_id': internal,
            'contact': arguments['actor'],
            'action': arguments['action'],
            'operation_id': arguments['operation_id'],
            'loan_container': arguments['loan_container'],
            'loan_id': arguments['loan_id'],
            'config_version': arguments['config_version'],
        }
        self.stages[arguments['operation_id']] = meta
        self.by_task[task_id] = meta
        return todo_ok_envelope(result={'taskId': task_id, 'todoDetailModel': deepcopy(detail)})

    def _todo_get(self, arguments):
        task_id = arguments['task_id']
        if task_id not in self.todos:
            return todo_error_envelope('TASK_NOT_EXIST')
        todo = self.todos[task_id]
        return todo_ok_envelope(result={'todoDetailModel': deepcopy(todo['detail'])})

    def _row_list(self, arguments):
        """One container result table, every row, read-shaped.

        Both the application table (issue #78) and the return form's table (issue #87)
        are listed through this one command: the transport never filters rows.
        Live ``record query --all`` on an empty table returns ``records: null``
        with ``hasMore: false``; that is what "no rows yet" looks like.
        """
        tenant = arguments['tenant_id']
        container = arguments['container_id']
        rows = []
        for (record_tenant, record_container, resource_id), cells in sorted(self.records.items()):
            if record_tenant != tenant or record_container != container:
                continue
            rows.append({'recordId': resource_id, 'cells': self._live_cells(cells)})
        return ok_envelope(records=rows or None, hasMore=False)

    def _loan_create(self, arguments):
        """Create a ledger row from the write payload; live ids are server-side."""
        self._loans += 1
        record_id = f'SYNTHETIC-loan-{self._loans:04d}'
        key = (arguments['tenant_id'], arguments['container_id'], record_id)
        self.records[key] = dict(arguments['cells'])
        return ok_envelope(data={'newRecordIds': [record_id]})

    def _loan_find_application(self, arguments):
        """Rows whose application-evidence cell equals the marker (server-side eq)."""
        tenant = arguments['tenant_id']
        container = arguments['container_id']
        field_id = self.fields.application_evidence
        rows = []
        for (record_tenant, record_container, resource_id), cells in sorted(self.records.items()):
            if record_tenant != tenant or record_container != container:
                continue
            if cells.get(field_id) == arguments['marker']:
                rows.append({'recordId': resource_id, 'cells': self._live_cells(cells)})
        return ok_envelope(records=rows or None, hasMore=False)

    def _loan_query_borrowed(self, arguments):
        tenant = arguments['tenant_id']
        container = arguments['loan_container']
        borrower = arguments['borrower']
        matches = []
        for (record_tenant, record_container, resource_id), cells in self.records.items():
            if record_tenant != tenant or record_container != container:
                continue
            ref = Resource('record', tenant, container, resource_id)
            try:
                current = decode_loan(ref, cells, self.fields)
            except Exception:
                continue
            if current.state == State.BORROWED and current.borrower.user_id == borrower:
                matches.append(resource_id)
        return ok_envelope(result={'loan_ids': sorted(matches)})

    def _title_names(self, arguments):
        """标题显示名；测试可预置 ``title_names`` 或让某次查询直接失败。"""
        if self.title_names_code is not None:
            return error_envelope(self.title_names_code)
        return ok_envelope(result=dict(self.title_names))

    def _stage_query(self, arguments):
        if 'operation_id' in arguments:
            meta = self.stages.get(arguments['operation_id'])
        else:
            meta = self.by_task.get(arguments.get('task_id'))
        if meta is None:
            return ok_envelope(result={})
        return ok_envelope(result=dict(meta))
