"""Subprocess double for dws.js. Returns preset JSON; never calls DingTalk.

Module name is unique so T09 file-based discovery does not collide. State is a
JSON file in FAKE_DWS_STATE. SYNTHETIC identifiers only.

The state file must carry the table schema (``kinds``: field id -> live cell shape,
built with ``t03_live_cells.declared_kinds``). It is what makes reads look like the
platform: this double stores the **write** payload, while a live read returns
materialized cells (singleSelect option names as ``{id, name}``, person cells as
``[{corpId, userId}]``, numbers as strings). A record query against a state without a
declared ``kinds`` is refused on stderr with a non-zero exit: the only thing this
double could do then is echo write payloads back, which is the loose pre-#33 double
(#73). An unknown shape name in the declaration is refused for the same reason.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from t03_live_cells import KindsError, filter_value, live_cells, validate_state_kinds


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


def todo_ok(**extra):
    payload = {
        'success': True,
        'errorCode': None,
        'errorMsg': None,
        'arguments': [],
    }
    payload.update(extra)
    return payload


def todo_err(code, message='SYNTHETIC-denied'):
    return {
        'success': False,
        'errorCode': code,
        'errorMsg': message,
        'arguments': [],
        'result': None,
    }


def load_state(path):
    if not path.exists():
        return {
            'records': {},
            'todos': {},
            'fail': {},
            'timeout': [],
            'late_write': [],
            'calls': [],
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


def filter_pairs(raw):
    """Flatten an ``and``/``eq`` aitable filter JSON into ``(field, value)`` pairs."""
    pairs = []

    def walk(node):
        if isinstance(node, dict):
            operator = node.get('operator')
            operands = node.get('operands') or []
            if operator == 'eq' and len(operands) == 2:
                pairs.append((operands[0], operands[1]))
                return
            for item in operands:
                walk(item)

    walk(json.loads(raw))
    return pairs


BOOLEAN = {'--all', '--yes'}

SPECS = {
    'auth status': {
        'required': {'--format'},
        'optional': set(),
    },
    'aitable record query': {
        'required': {'--base-id', '--table-id', '--format'},
        'optional': {'--all', '--record-ids', '--filters', '--field-ids'},
        'one_of': ({'--record-ids', '--filters'},),
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
    'todo task list': {
        'required': {'--size', '--format'},
        'optional': {'--status'},
    },
    'todo task get': {
        'required': {'--task-id', '--format'},
        'optional': set(),
    },
    'contact user get': {
        'required': {'--ids', '--format'},
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


def verb_of(argv):
    """Longest command word count that names a known verb.

    ``todo task list`` is three words, ``auth status`` is two; the flag offset
    follows from the verb itself so neither shape has to be special-cased by the
    caller.
    """
    for taken in (3, 2):
        verb = ' '.join(argv[:taken])
        if verb in SPECS:
            return verb
    return ' '.join(argv[:3])


def require_spec(verb, argv):
    spec = SPECS.get(verb)
    if spec is None:
        return err('UNSUPPORTED_COMMAND')
    flags, problem = parse_flags(argv[len(verb.split()):])
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


def synthetic_option_write(cells):
    for value in cells.values():
        if not isinstance(value, dict):
            continue
        option_id = value.get('id')
        if isinstance(option_id, str) and option_id.startswith('SYNTHETIC-opt-'):
            return True
    return False


def main(argv):
    verb = verb_of(argv)
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
        try:
            kinds = validate_state_kinds(state)
        except KindsError as exc:
            sys.stderr.write(
                f'fake dws 拒绝执行 record query：{exc}\n'
                '声明字段类型的 state 缺失或写错时，替身只能把写入载荷原样回给读侧。\n'
            )
            return 2
        base_id = flag(argv, '--base-id')
        table_id = flag(argv, '--table-id')
        record_id = flag(argv, '--record-ids')
        item = state['records'].get(key(base_id, table_id, record_id))
        records = [] if item is None else [{
            'recordId': record_id,
            'cells': live_cells(item, kinds),
        }]
        raw_filters = flag(argv, '--filters')
        if raw_filters:
            wanted = filter_pairs(raw_filters)
            field_ids = [f for f in (flag(argv, '--field-ids') or '').split(',') if f]
            prefix = f'{base_id}/{table_id}/'
            filtered = []
            for slot, cells in sorted(state['records'].items()):
                if not slot.startswith(prefix):
                    continue
                live = live_cells(cells, kinds)
                if not all(filter_value(kinds, field, live.get(field)) == value
                           for field, value in wanted):
                    continue
                kept = {f: live[f] for f in field_ids if f in live} if field_ids else live
                filtered.append({'recordId': slot[len(prefix):], 'cells': kept})
            print(json.dumps({'hasMore': bool(state.get('query_truncated')),
                              'pages': 1,
                              'records': filtered or None},
                             ensure_ascii=True))
            return 0
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
        state['todos'][task_id] = {'detail': detail,
                                   'subject': flag(argv, '--title'),
                                   'executor_contact': flag(argv, '--executors')}
        save_state(state_path, state)
        if 'todo task create' in state.get('late_write', ()):
            # The live write can land while the envelope never comes back.
            time.sleep(120)
        print(json.dumps(todo_ok(result={'taskId': task_id, 'todoDetailModel': detail}),
                         ensure_ascii=True))
        return 0
    if argv[:2] == ['auth', 'status']:
        # Live shape: a flat object with authenticated/token_valid/user_id.
        if state.get('auth_unauthenticated'):
            print(json.dumps({'success': True, 'authenticated': False,
                              'token_valid': False}, ensure_ascii=True))
            return 0
        print(json.dumps(ok(authenticated=True, token_valid=True,
                            user_id=state.get('login_user'),
                            user_name='SYNTHETIC-login'), ensure_ascii=True))
        return 0
    if argv[:3] == ['todo', 'task', 'list']:
        size = int(flag(argv, '--size'))
        status_flag = flag(argv, '--status')
        login_user = state.get('login_user')
        # Every list call is written down: the recovery path must never lean on
        # the undocumented default of ``--status``.
        state.setdefault('calls', []).append({
            'verb': 'todo task list', 'size': size, 'status': status_flag,
            'login_user': login_user,
        })
        save_state(state_path, state)
        cards = []
        for task_id, todo in state['todos'].items():
            detail = todo.get('detail') or {}
            if status_flag is not None:
                want_done = status_flag.lower() == 'true'
                if bool(detail.get('isDone')) != want_done:
                    continue
            if login_user is not None:
                if todo.get('executor_contact') != login_user:
                    continue
            cards.append({
                'subject': todo.get('subject'),
                'taskId': task_id,
                'createdTime': 0,
                'dueTime': 0,
                'finalStatusStage': 0,
                'priority': 0,
            })
        if size > 20:
            # Live: ``--size`` above one page makes the CLI page and merge by
            # itself, so the merged answer carries no ``hasMore``/``nextToken``.
            result = {'todoCards': cards}
        else:
            result = {'todoCards': cards[:size]}
            if len(cards) > size:
                result['hasMore'] = True
        if state.get('list_more'):
            result['hasMore'] = True
        print(json.dumps(todo_ok(result=result), ensure_ascii=True))
        return 0
    if argv[:3] == ['todo', 'task', 'get']:
        task_id = flag(argv, '--task-id')
        todo = state['todos'].get(task_id)
        if todo is None:
            print(json.dumps(todo_err('TASK_NOT_EXIST'), ensure_ascii=True))
            return 0
        print(json.dumps(todo_ok(result={'todoDetailModel': todo['detail']}),
                         ensure_ascii=True))
        return 0
    if argv[:3] == ['contact', 'user', 'get']:
        # 显示名只认 state['contact_names'] 里点过名的 userId；没点名的查不到。
        names = state.get('contact_names') or {}
        rows = []
        for user_id in (flag(argv, '--ids') or '').split(','):
            name = names.get(user_id.strip()) if user_id.strip() else None
            if not name:
                continue
            rows.append({'orgEmployeeModel': {'orgUserName': name}})
        print(json.dumps(ok(result=rows), ensure_ascii=True))
        return 0
    if argv[:3] == ['chat', 'message', 'send']:
        print(json.dumps(ok(result={'openTaskId': 'SYNTHETIC-chat'}),
                         ensure_ascii=True))
        return 0
    print(json.dumps(err('UNSUPPORTED_COMMAND'), ensure_ascii=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
