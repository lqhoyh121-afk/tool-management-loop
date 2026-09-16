"""Subprocess double for dws.js. Returns preset JSON; never calls DingTalk.

Module name is unique so T09 file-based discovery does not collide. State is a
JSON file in FAKE_DWS_STATE. SYNTHETIC identifiers only.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def ok(**extra):
    payload = {'success': True, 'status': 'success', 'error': {}}
    payload.update(extra)
    return payload


def err(code, message='SYNTHETIC-denied'):
    return {
        'success': True,
        'status': 'error',
        'error': {'code': code, 'message': message},
    }


def load_state(path):
    if not path.exists():
        return {
            'records': {},
            'todos': {},
            'fail': {},
            'timeout': [],
            'seq': {'form': 0, 'todo': 0, 'internal': 9000000100},
        }
    return json.loads(path.read_text(encoding='utf-8'))


def save_state(path, state):
    path.write_text(json.dumps(state, ensure_ascii=True), encoding='utf-8')


def flag(argv, name):
    if name in argv:
        return argv[argv.index(name) + 1]
    return None


def has(argv, name):
    return name in argv


BOOLEAN = {'--all', '--yes'}

SPECS = {
    'aitable record query': {
        'required': {'--base-id', '--table-id', '--record-ids', '--format'},
        'optional': {'--all'},
    },
    'aitable record update': {
        'required': {'--base-id', '--table-id', '--records-file', '--yes', '--format'},
        'optional': set(),
    },
    'aitable record create': {
        'required': {'--base-id', '--table-id', '--records-file', '--yes', '--format'},
        'optional': set(),
    },
    'todo task create': {
        'required': {'--title', '--executors', '--yes', '--format'},
        'optional': set(),
    },
    'todo task get': {
        'required': {'--task-id', '--format'},
        'optional': set(),
    },
    'chat message send': {
        'required': {'--title', '--text', '--yes', '--format'},
        'optional': {'--user', '--open-dingtalk-id'},
        'one_of': ({'--user', '--open-dingtalk-id'},),
    },
}


def parse_flags(argv):
    flags = {}
    i = 0
    while i < len(argv):
        name = argv[i]
        if not isinstance(name, str) or not name.startswith('--'):
            return None, f'unexpected positional {name!r}'
        if name in BOOLEAN:
            flags[name] = True
            i += 1
            continue
        if i + 1 >= len(argv) or str(argv[i + 1]).startswith('--'):
            return None, f'missing value for {name}'
        flags[name] = argv[i + 1]
        i += 2
    return flags, None


def require_spec(verb, argv):
    spec = SPECS.get(verb)
    if spec is None:
        return err('UNSUPPORTED_COMMAND')
    flags, problem = parse_flags(argv[3:])
    if problem:
        return err('UNKNOWN_FLAG', problem)
    allowed = spec['required'] | spec['optional']
    unknown = set(flags) - allowed
    if unknown:
        return err('UNKNOWN_FLAG', sorted(unknown)[0])
    missing = spec['required'] - set(flags)
    if missing:
        return err('MISSING_FLAG', sorted(missing)[0])
    for group in spec.get('one_of', ()):
        present = [name for name in group if name in flags]
        if len(present) != 1:
            return err('MISSING_FLAG', sorted(group)[0])
    return None


def key(base_id, table_id, record_id):
    return f'{base_id}/{table_id}/{record_id}'


_SYNTHETIC_SELECT_FIELDS = frozenset({'fldSYN-state', 'fldSYN-tracked'})


def _live_cells(cells):
    visible = {}
    seq = 0
    for field_id, value in cells.items():
        if value == '':
            continue
        if isinstance(value, dict) and 'id' in value and 'name' in value:
            seq += 1
            visible[field_id] = {
                'id': f'SYNTHETIC-rand-{seq:04d}',
                'name': value['name'],
            }
        elif field_id in _SYNTHETIC_SELECT_FIELDS and isinstance(value, str):
            seq += 1
            visible[field_id] = {
                'id': f'SYNTHETIC-rand-{seq:04d}',
                'name': value,
            }
        else:
            visible[field_id] = value
    return visible


def synthetic_option_write(cells):
    for value in cells.values():
        if not isinstance(value, dict):
            continue
        option_id = value.get('id')
        if isinstance(option_id, str) and option_id.startswith('SYNTHETIC-opt-'):
            return True
    return False


def main(argv):
    verb = ' '.join(argv[:3])
    spec_error = require_spec(verb, argv)
    if spec_error:
        print(json.dumps(spec_error, ensure_ascii=True))
        return 0
    state_path = Path(os.environ['FAKE_DWS_STATE'])
    state = load_state(state_path)
    if verb in state.get('timeout', []):
        time.sleep(120)
    fail = state.get('fail', {}).get(verb)
    if fail:
        print(json.dumps(err(fail), ensure_ascii=True))
        return 0
    if argv[:3] == ['aitable', 'record', 'query']:
        base_id = flag(argv, '--base-id')
        table_id = flag(argv, '--table-id')
        record_id = flag(argv, '--record-ids')
        item = state['records'].get(key(base_id, table_id, record_id))
        records = [] if item is None else [{
            'recordId': record_id,
            'cells': _live_cells(item),
        }]
        if has(argv, '--all'):
            print(json.dumps(ok(records=records, hasMore=False), ensure_ascii=True))
        else:
            print(json.dumps(ok(data={'records': records, 'hasMore': False}),
                             ensure_ascii=True))
        return 0
    if argv[:3] == ['aitable', 'record', 'update']:
        records = json.loads(Path(flag(argv, '--records-file')).read_text(encoding='utf-8'))
        record = records[0]
        base_id = flag(argv, '--base-id')
        table_id = flag(argv, '--table-id')
        slot = key(base_id, table_id, record['recordId'])
        if slot not in state['records']:
            print(json.dumps(err('RECORD_NOT_FOUND'), ensure_ascii=True))
            return 0
        cells = dict(record['cells'])
        if synthetic_option_write(cells):
            print(json.dumps(err('SELECT_OPTION_NOT_FOUND'), ensure_ascii=True))
            return 0
        state['records'][slot] = cells
        save_state(state_path, state)
        print(json.dumps(ok(data={'recordIds': [record['recordId']]}),
                         ensure_ascii=True))
        return 0
    if argv[:3] == ['aitable', 'record', 'create']:
        records = json.loads(Path(flag(argv, '--records-file')).read_text(encoding='utf-8'))
        state['seq']['form'] += 1
        form_id = f'SYNTHETIC-form-{state["seq"]["form"]:04d}'
        base_id = flag(argv, '--base-id')
        table_id = flag(argv, '--table-id')
        cells = dict(records[0]['cells'])
        if synthetic_option_write(cells):
            print(json.dumps(err('SELECT_OPTION_NOT_FOUND'), ensure_ascii=True))
            return 0
        state['records'][key(base_id, table_id, form_id)] = cells
        save_state(state_path, state)
        print(json.dumps(ok(data={'newRecordIds': [form_id]}), ensure_ascii=True))
        return 0
    if argv[:3] == ['todo', 'task', 'create']:
        state['seq']['todo'] += 1
        state['seq']['internal'] += 1
        task_id = f'SYNTHETIC-todo-{state["seq"]["todo"]:04d}'
        internal = state['seq']['internal']
        detail = {
            'taskId': task_id,
            'isDone': False,
            'finishTime': 0,
            'executorIds': [internal],
            'activities': [],
        }
        state['todos'][task_id] = {'detail': detail, 'occurred_at': None}
        save_state(state_path, state)
        print(json.dumps(ok(result={'taskId': task_id, 'todoDetailModel': detail}),
                         ensure_ascii=True))
        return 0
    if argv[:3] == ['todo', 'task', 'get']:
        task_id = flag(argv, '--task-id')
        todo = state['todos'].get(task_id)
        if todo is None:
            print(json.dumps(err('TASK_NOT_EXIST'), ensure_ascii=True))
            return 0
        result = {'todoDetailModel': todo['detail']}
        if todo.get('occurred_at'):
            result['occurredAt'] = todo['occurred_at']
        print(json.dumps(ok(result=result), ensure_ascii=True))
        return 0
    if argv[:3] == ['chat', 'message', 'send']:
        print(json.dumps(ok(result={'openTaskId': 'SYNTHETIC-chat'}),
                         ensure_ascii=True))
        return 0
    print(json.dumps(err('UNSUPPORTED_COMMAND'), ensure_ascii=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
