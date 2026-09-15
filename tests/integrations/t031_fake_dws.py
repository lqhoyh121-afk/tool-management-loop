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


def key(base_id, table_id, record_id):
    return f'{base_id}/{table_id}/{record_id}'


def main(argv):
    state_path = Path(os.environ['FAKE_DWS_STATE'])
    state = load_state(state_path)
    verb = ' '.join(argv[:3])
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
            'cells': item,
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
        state['records'][slot] = dict(record['cells'])
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
        state['records'][key(base_id, table_id, form_id)] = dict(records[0]['cells'])
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
