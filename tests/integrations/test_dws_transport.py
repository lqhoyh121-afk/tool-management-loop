"""Isolation tests for DwsTransport. Fake subprocess only; no live DingTalk."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t031_fake_dws import load_state, save_state
from t03_layout import entry_fields_from
from t03_live_cells import SINGLE_SELECT, declared_kinds
from t03_memory_transport import MemoryTransport

from contracts.flow import plan, verify
from contracts.model import (Action, Code, ContractError, Identity, Outcome,
                             Resource, State)
from contracts.ports import (LedgerScope, RuntimeBinding, StageRequest,
                             stage_operation_id, verify_stage)
from integrations.dingtalk.adapter import DingTalkAdapter, stage_title
from integrations.dingtalk.codec import (_put_identity, encode_inventory,
                                         encode_loan)
from integrations.dingtalk.dws_transport import (SCOPE_OTHER, SCOPE_SELF,
                                                 SCOPE_UNKNOWN,
                                                 RECOVERY_NOT_ATTEMPTED,
                                                 RECOVERY_NOT_BUILT,
                                                 RECOVERY_NEEDS_MANUAL,
                                                 REASON_EXECUTOR_SCOPE,
                                                 REASON_LIST_UNREADABLE,
                                                 REASON_TITLE_AMBIGUOUS,
                                                 DwsTransport,
                                                 pending_stage_report,
                                                 split_container,
                                                 todo_internal_id,
                                                 windows_native_path)
from integrations.dingtalk.envelope import extract_records, record_cells
from integrations.dingtalk.errors import UnsupportedShapeError
from integrations.dingtalk.layout import SYNTHETIC_FIELDS, SYNTHETIC_RETURN_FORM_FIELDS


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t031_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t031_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
MANAGER = _fixtures.MANAGER
SyntheticJournal, SyntheticLease = _synthetic.SyntheticJournal, _synthetic.SyntheticLease

LOAN = Resource('record', 'synthetic-org', 'baseLoan/tblLoan', 'recLoan')
ITEM = Resource('record', 'synthetic-org', 'baseStock/tblStock', 'recItem')
FORM_CONTAINER = 'baseForm/tblForm'
TODO_CONTAINER = 'todoSpace/executors'
FAKE_DWS = Path(__file__).resolve().parent / 't031_fake_dws.py'


def loan():
    original = _fixtures.loan()
    return replace(original, ref=LOAN, item=ITEM)


def stock():
    return replace(_fixtures.stock(), ref=ITEM)


def event(action):
    return replace(_fixtures.event(action), loan_ref=LOAN)


class DwsTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.state_path = self.work / 'fake-state.json'
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = entry_fields_from(self.fields)
        state = load_state(self.state_path)
        state['kinds'] = declared_kinds(self.fields, self.entry_fields)
        save_state(self.state_path, state)
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = DwsTransport(
            [sys.executable, str(FAKE_DWS)], self.fields,
            form_container=FORM_CONTAINER, todo_container=TODO_CONTAINER,
            work_dir=self.work / 'runtime', entry_fields=self.entry_fields,
            extra_env={'FAKE_DWS_STATE': str(self.state_path)},
        )
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields)
        self.seed(loan(), stock())

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, current, inventory):
        state = load_state(self.state_path)
        state['records'][f'{current.ref.container_id}/{current.ref.resource_id}'] = (
            encode_loan(current, self.fields))
        state['records'][f'{inventory.ref.container_id}/{inventory.ref.resource_id}'] = (
            encode_inventory(inventory, self.fields))
        save_state(self.state_path, state)

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_container_split_and_rejects_fixture_names(self):
        self.assertEqual(split_container('baseX/tblY'), ('baseX', 'tblY'))
        with self.assertRaises(UnsupportedShapeError):
            split_container('synthetic-forms')

    def test_records_file_is_windows_native(self):
        native = windows_native_path(self.work / 'payload.json')
        self.assertEqual(native, os.path.abspath(native))
        if os.name == 'nt':
            self.assertNotIn('/', native)
            self.assertIn('\\', native)
            mixed = str(self.work / 'payload.json').replace('\\', '/')
            self.assertIn('/', mixed)
            self.assertNotIn('/', windows_native_path(mixed))

    def test_query_single_page_uses_data_records(self):
        payload = self.transport.exchange('record.query', {
            'tenant_id': LOAN.tenant_id,
            'container_id': LOAN.container_id,
            'resource_id': LOAN.resource_id,
        })
        self.assertIn('records', payload['data'])
        self.assertNotIn('records', payload)
        listed = self.transport.exchange('record.query', {
            'tenant_id': LOAN.tenant_id,
            'container_id': LOAN.container_id,
            'resource_id': LOAN.resource_id,
            'all': True,
        })
        self.assertIn('records', listed)
        self.assertNotIn('records', listed.get('data') or {})

    def test_synthetic_option_id_write_is_not_success(self):
        current = loan()
        cells = encode_loan(current, self.fields)
        cells[self.fields.state] = {
            'id': f'SYNTHETIC-opt-{current.state.value}',
            'name': current.state.value,
        }
        payload = self.transport.exchange('record.update', {
            'tenant_id': LOAN.tenant_id,
            'container_id': LOAN.container_id,
            'resource_id': LOAN.resource_id,
            'cells': cells,
        })
        self.assertEqual(payload['status'], 'error')
        self.assertEqual(payload['error']['code'], 'SELECT_OPTION_NOT_FOUND')
        self.assertEqual(self.adapter.read_loan(LOAN).quantity, 2)

    def test_update_uses_records_file_argument(self):
        current = loan()
        cells = encode_loan(replace(current, quantity=1), self.fields)
        payload = self.transport.exchange('record.update', {
            'tenant_id': LOAN.tenant_id,
            'container_id': LOAN.container_id,
            'resource_id': LOAN.resource_id,
            'cells': cells,
        })
        self.assertEqual(payload['data']['recordIds'], [LOAN.resource_id])
        self.assertEqual(payload['status'], 'success')
        files = list((self.work / 'runtime' / 'dws-records-file').iterdir())
        self.assertEqual(len(files), 1)
        if os.name == 'nt':
            self.assertNotIn('/', os.path.abspath(str(files[0])))

    def test_timeout_returns_none_without_retry_write(self):
        state = load_state(self.state_path)
        state['timeout'] = ['aitable record update']
        save_state(self.state_path, state)
        self.transport.timeout = 0.3
        first = self.transport.exchange('record.update', {
            'tenant_id': LOAN.tenant_id,
            'container_id': LOAN.container_id,
            'resource_id': LOAN.resource_id,
            'cells': encode_loan(loan(), self.fields),
        })
        self.assertIsNone(first)
        self.assertEqual(self.adapter.read_loan(LOAN).quantity, 2)

    def test_forbidden_envelope_is_identity(self):
        state = load_state(self.state_path)
        state['fail'] = {'aitable record query': 'FORBIDDEN'}
        save_state(self.state_path, state)
        self.blocked(Code.IDENTITY, lambda: self.adapter.read_loan(LOAN))

    def test_status_error_with_success_true_is_not_success(self):
        state = load_state(self.state_path)
        state['fail'] = {'aitable record query': 'BASE_NOT_FOUND'}
        save_state(self.state_path, state)
        self.blocked(Code.UNKNOWN, lambda: self.adapter.read_loan(LOAN))

    def test_fake_dws_query_materializes_string_single_select(self):
        state = load_state(self.state_path)
        slot = f'{LOAN.container_id}/{LOAN.resource_id}'
        stored = state['records'][slot]
        self.assertIsInstance(stored[self.fields.state], str)
        payload = self.transport.exchange('record.query', {
            'tenant_id': LOAN.tenant_id,
            'container_id': LOAN.container_id,
            'resource_id': LOAN.resource_id,
        })
        cells = payload['data']['records'][0]['cells']
        self.assertIsInstance(cells[self.fields.state], dict)
        self.assertIn('id', cells[self.fields.state])
        self.assertEqual(cells[self.fields.state]['name'], stored[self.fields.state])
        self.assertNotEqual(cells[self.fields.state]['id'], cells[self.fields.state]['name'])
        self.assertEqual(self.adapter.read_loan(LOAN).state, loan().state)

    def _live(self, ref):
        payload = self.transport.exchange('record.query', {
            'tenant_id': ref.tenant_id,
            'container_id': ref.container_id,
            'resource_id': ref.resource_id,
        })
        return record_cells(extract_records(payload)[0])

    def _store(self, ref, cells):
        state = load_state(self.state_path)
        state['records'][f'{ref.container_id}/{ref.resource_id}'] = cells
        save_state(self.state_path, state)

    def test_fake_dws_materializes_person_and_number_cells(self):
        """The read shape is not the write payload for persons and numbers either."""
        state = load_state(self.state_path)
        slot = f'{LOAN.container_id}/{LOAN.resource_id}'
        cells = dict(state['records'][slot])
        cells[self.fields.quantity] = 2
        cells[self.fields.borrower] = [
            {'corpId': 'synthetic-org', 'userId': 'synthetic-user', 'name': '合成姓名'},
        ]
        self._store(LOAN, cells)
        live = self._live(LOAN)
        self.assertEqual(live[self.fields.quantity], '2')
        self.assertEqual(live[self.fields.borrower],
                         [{'corpId': 'synthetic-org', 'userId': 'synthetic-user'}])

    def test_fake_dws_materializes_a_declared_select_outside_the_old_pair(self):
        """`fldSYN-decision` was never whitelisted; kinds now come from the schema."""
        row = dict(encode_loan(loan(), self.fields))
        row[self.entry_fields.loan_container] = LOAN.container_id
        row[self.entry_fields.loan_id] = LOAN.resource_id
        row[self.entry_fields.decision] = 'agree'
        row[self.entry_fields.occurred_at] = _fixtures.NOW.isoformat()
        form = Resource('form', LOAN.tenant_id, FORM_CONTAINER, 'recForm')
        self._store(form, row)
        live = self._live(form)
        self.assertEqual(live[self.entry_fields.decision]['name'], 'agree')
        self.assertNotEqual(live[self.entry_fields.decision]['id'], 'agree')
        event = self.adapter.read_event(loan(), form)
        self.assertEqual(event.action, Action.APPROVE)
        self.assertEqual(event.actor, loan().approver)

    def test_fake_dws_option_id_is_stable_per_option_not_per_read(self):
        other = Resource('record', LOAN.tenant_id, LOAN.container_id, 'recOther')
        self._seed_row(other, state=loan().state)
        first = self._live(LOAN)[self.fields.state]['id']
        self.assertEqual(self._live(LOAN)[self.fields.state]['id'], first)
        self.assertEqual(self._live(other)[self.fields.state]['id'], first)
        self.assertNotEqual(self._live(LOAN)[self.fields.tracked]['id'], first)

    def test_fake_dws_filters_empty_result_is_null_not_an_empty_list(self):
        """Live `--all` answers an empty filter with `records: null`, not `[]`."""
        borrower = encode_loan(loan(), self.fields)[self.fields.borrower][0]['userId']
        argv = [sys.executable, str(FAKE_DWS), 'aitable', 'record', 'query',
                '--base-id', 'baseLoan', '--table-id', 'tblLoan',
                '--filters', json.dumps({'operator': 'and', 'operands': [
                    {'operator': 'eq', 'operands': [self.fields.state, 'borrowed']}]}),
                '--field-ids', f'{self.fields.state},{self.fields.borrower}',
                '--all', '--format', 'json']
        env = dict(os.environ, FAKE_DWS_STATE=str(self.state_path))
        out = subprocess.run(argv, capture_output=True, text=True, env=env)
        payload = json.loads(out.stdout)
        self.assertIn('records', payload)
        self.assertIsNone(payload['records'])
        self.assertFalse(payload['hasMore'])
        self.assertEqual(self._borrowed_query(borrower)['result']['loan_ids'], [])

    def test_both_doubles_return_the_same_live_cells(self):
        """The file double and MemoryTransport must not drift on read shapes (#33)."""
        state = load_state(self.state_path)
        memory = MemoryTransport(self.fields, self.entry_fields)
        for ref in (LOAN, ITEM):
            memory.seed_record(ref, state['records'][f'{ref.container_id}/{ref.resource_id}'])
            payload = memory.exchange('record.query', {
                'tenant_id': ref.tenant_id,
                'container_id': ref.container_id,
                'resource_id': ref.resource_id,
            })
            self.assertEqual(record_cells(extract_records(payload)[0]), self._live(ref))

    def test_read_submit_query_roundtrip(self):
        self.assertEqual(self.adapter.read_loan(LOAN).ref, LOAN)
        self.assertEqual(self.adapter.read_inventory(ITEM).available, 5)
        intent = plan(loan(), event(Action.APPROVE), stock())
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(verify(intent, receipt).outcome, Outcome.VERIFIED)
        self.assertEqual(self.adapter.query(intent).loan.state, State.RESERVATION_PENDING)

    def test_timeout_submit_retries_when_records_unchanged(self):
        state = load_state(self.state_path)
        state['timeout'] = ['aitable record update']
        save_state(self.state_path, state)
        self.transport.timeout = 0.3
        intent = plan(loan(), event(Action.APPROVE), stock())
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.UNKNOWN)
        state['timeout'] = []
        save_state(self.state_path, state)
        retried = self.adapter.submit(intent, self.binding, self.lease)
        self.assertEqual(verify(intent, retried).outcome, Outcome.VERIFIED)

    def test_create_stage_form_and_todo_use_query_container(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        form = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(form.outcome, Outcome.VERIFIED)
        self.assertEqual(form.source.container_id, FORM_CONTAINER)
        current = loan()
        inventory = stock()
        intent = plan(current, event(Action.APPROVE), inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current, inventory = verify(intent, receipt).loan, receipt.inventory
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        intent = plan(current, reserve, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current = verify(intent, receipt).loan
        issue = StageRequest(stage_operation_id(current, Action.ISSUE),
                             current, Action.ISSUE, MANAGER)
        todo = self.adapter.create_stage(issue, self.binding, self.lease)
        self.assertEqual(todo.outcome, Outcome.VERIFIED)
        self.assertEqual(todo.source.container_id, TODO_CONTAINER)

    def test_approve_stage_nudges_the_approver_and_keeps_the_entry_row(self):
        # 真源 = 入口行：催办待办另用一个操作号，不能顶掉阶段索引里的入口行。
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(receipt.outcome, Outcome.VERIFIED)
        self.assertEqual(receipt.source.container_id, FORM_CONTAINER)
        queried = self.transport.exchange('stage.query', {
            'operation_id': request.operation_id,
        })
        self.assertEqual(queried['result']['kind'], 'form')
        self.assertEqual(queried['result']['resource_id'], receipt.source.resource_id)

        state = load_state(self.state_path)
        self.assertEqual(len(state['todos']), 1)
        task_id = next(iter(state['todos']))
        nudge = self.transport.exchange('stage.query', {'task_id': task_id})
        self.assertEqual(nudge['result']['kind'], 'todo')
        self.assertEqual(nudge['result']['action'], Action.APPROVE.value)
        self.assertEqual(nudge['result']['contact'], MANAGER.user_id)

    def test_chat_send_is_mapped(self):
        payload = self.transport.exchange('chat.send', {
            'user': 'synthetic-manager',
            'title': 'SYNTHETIC-title',
            'text': 'SYNTHETIC-notice',
        })
        self.assertEqual(payload['result']['openTaskId'], 'SYNTHETIC-chat')
        fallback = self.transport.exchange('chat.send', {
            'open_dingtalk_id': 'SYNTHETIC-open-id',
            'title': 'SYNTHETIC-title',
            'text': 'SYNTHETIC-notice',
        })
        self.assertEqual(fallback['result']['openTaskId'], 'SYNTHETIC-chat')

    def test_fake_dws_rejects_unknown_chat_flags(self):
        from t031_fake_dws import require_spec
        argv = ['chat', 'message', 'send', '--to', 'x', '--content', 'y',
                '--yes', '--format', 'json']
        payload = require_spec('chat message send', argv)
        self.assertEqual(payload['error']['code'], 'UNKNOWN_FLAG')
        missing = require_spec('chat message send', [
            'chat', 'message', 'send', '--user', 'synthetic-manager',
            '--text', 'SYNTHETIC-notice', '--yes', '--format', 'json'])
        self.assertEqual(missing['error']['code'], 'MISSING_FLAG')

    def test_todo_internal_id_rejects_contact_shape(self):
        self.assertIsNone(todo_internal_id('20250331084503014-F4C7-648B2A8D2'))
        self.assertEqual(todo_internal_id(9000000101), '9000000101')
        self.assertEqual(todo_internal_id('9000000101'), '9000000101')

    def test_todo_create_stage_stores_internal_id_from_get(self):
        current = loan()
        inventory = stock()
        intent = plan(current, event(Action.APPROVE), inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current, inventory = verify(intent, receipt).loan, receipt.inventory
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        intent = plan(current, reserve, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current = verify(intent, receipt).loan
        issue = StageRequest(stage_operation_id(current, Action.ISSUE),
                             current, Action.ISSUE, MANAGER)
        todo = self.adapter.create_stage(issue, self.binding, self.lease)
        queried = self.transport.exchange('stage.query', {
            'task_id': todo.source.resource_id,
        })
        internal = queried['result']['internal_id']
        self.assertNotIn('-', str(internal))

    def test_todo_stage_keeps_claimed_task_id_when_post_create_get_unavailable(self):
        original = self.transport._todo_detail_for_stage
        self.transport._todo_detail_for_stage = lambda task_id: None
        try:
            payload = self.transport.exchange('todo.create', {
                'tenant_id': LOAN.tenant_id,
                'actor': MANAGER.user_id,
                'action': Action.ISSUE.value,
                'operation_id': 'SYNTHETIC-op-no-stage',
                'loan_container': LOAN.container_id,
                'loan_id': LOAN.resource_id,
                'config_version': 'synthetic-config-v1',
            })
            task_id = payload['result']['taskId']
            queried = self.transport.exchange('stage.query', {'task_id': task_id})
            self.assertTrue(queried['result']['pending'])
            self.assertEqual(queried['result']['claimed_task_id'], task_id)
            self.assertEqual(queried['result']['executor_contact'], MANAGER.user_id)
        finally:
            self.transport._todo_detail_for_stage = original

    def test_claimed_task_id_recovers_via_get_when_list_is_executor_scoped(self):
        request = self._issue_request()
        original = self.transport._todo_detail_for_stage
        self.transport._todo_detail_for_stage = lambda task_id: None
        try:
            payload = self.transport.exchange('todo.create', {
                'tenant_id': LOAN.tenant_id,
                'actor': MANAGER.user_id,
                'action': request.action.value,
                'operation_id': request.operation_id,
                'loan_container': LOAN.container_id,
                'loan_id': LOAN.resource_id,
                'config_version': 'synthetic-config-v1',
            })
            task_id = payload['result']['taskId']
        finally:
            self.transport._todo_detail_for_stage = original
        state = load_state(self.state_path)
        state['login_user'] = 'synthetic-other-login'
        save_state(self.state_path, state)
        title = stage_title(request.loan, request.action)
        self.assertIsNone(self.transport._todo_task_id_by_title(title))
        queried = self.transport.exchange('stage.query', {'operation_id': request.operation_id})
        self.assertFalse(queried['result'].get('pending'))
        self.assertEqual(queried['result']['resource_id'], task_id)
        self.assertIn('internal_id', queried['result'])
        self.assertEqual(queried['result']['creation_evidence'], f'todo.task.create:{task_id}')

    def _set_login(self, user_id):
        state = load_state(self.state_path)
        state['login_user'] = user_id
        save_state(self.state_path, state)

    def _todo_list_calls(self):
        return [call for call in load_state(self.state_path).get('calls', [])
                if call['verb'] == 'todo task list']

    def test_pending_stage_records_the_actor_versus_login_relation(self):
        """A pending todo remembers whose todo list may hold it (#72)."""
        self._set_login(MANAGER.user_id)
        original = self.transport._todo_detail_for_stage
        self.transport._todo_detail_for_stage = lambda task_id: None
        try:
            payload = self.transport.exchange('todo.create', {
                'tenant_id': LOAN.tenant_id,
                'actor': MANAGER.user_id,
                'action': Action.ISSUE.value,
                'operation_id': 'SYNTHETIC-op-scope-self',
                'loan_container': LOAN.container_id,
                'loan_id': LOAN.resource_id,
                'config_version': 'synthetic-config-v1',
            })
            # Read the pending record while the post-create read is still
            # unavailable: this is the state the annotation is written for.
            queried = self.transport.exchange(
                'stage.query', {'task_id': payload['result']['taskId']})['result']
        finally:
            self.transport._todo_detail_for_stage = original
        self.assertTrue(queried['pending'])
        self.assertEqual(queried['executor_contact'], MANAGER.user_id)
        self.assertEqual(queried['login_user_id'], MANAGER.user_id)
        self.assertEqual(queried['executor_scope'], SCOPE_SELF)
        self.assertEqual(queried['recovery_state'], RECOVERY_NOT_ATTEMPTED)

    def test_pending_stage_records_a_login_that_is_not_the_actor(self):
        """actor ≠ login account is written down, not discovered later (#72)."""
        self._set_login('SYNTHETIC-other-login')
        original = self.transport._todo_detail_for_stage
        self.transport._todo_detail_for_stage = lambda task_id: None
        try:
            payload = self.transport.exchange('todo.create', {
                'tenant_id': LOAN.tenant_id,
                'actor': MANAGER.user_id,
                'action': Action.ISSUE.value,
                'operation_id': 'SYNTHETIC-op-scope-other',
                'loan_container': LOAN.container_id,
                'loan_id': LOAN.resource_id,
                'config_version': 'synthetic-config-v1',
            })
            queried = self.transport.exchange(
                'stage.query', {'task_id': payload['result']['taskId']})['result']
        finally:
            self.transport._todo_detail_for_stage = original
        self.assertEqual(queried['executor_scope'], SCOPE_OTHER)
        self.assertEqual(queried['login_user_id'], 'SYNTHETIC-other-login')

    def test_unobservable_login_state_is_unknown_scope_not_a_guess(self):
        """An unauthenticated CLI records 'unknown', never 'self' (#72)."""
        state = load_state(self.state_path)
        state['auth_unauthenticated'] = True
        save_state(self.state_path, state)
        original = self.transport._todo_detail_for_stage
        self.transport._todo_detail_for_stage = lambda task_id: None
        try:
            payload = self.transport.exchange('todo.create', {
                'tenant_id': LOAN.tenant_id,
                'actor': MANAGER.user_id,
                'action': Action.ISSUE.value,
                'operation_id': 'SYNTHETIC-op-scope-unknown',
                'loan_container': LOAN.container_id,
                'loan_id': LOAN.resource_id,
                'config_version': 'synthetic-config-v1',
            })
            quarantined = self.transport.exchange(
                'stage.query', {'task_id': payload['result']['taskId']})['result']
        finally:
            self.transport._todo_detail_for_stage = original
        self.assertEqual(quarantined['executor_scope'], SCOPE_UNKNOWN)
        self.assertIsNone(quarantined['login_user_id'])

    def test_todo_recovery_merges_both_completion_statuses(self):
        """Both ``--status`` predicates are read, so a done todo is still found."""
        self._set_login(MANAGER.user_id)
        state = load_state(self.state_path)
        self._seed_todo(state, 'SYNTHETIC-todo-9001', 'stage:open')
        self._seed_todo(state, 'SYNTHETIC-todo-9002', 'stage:done')
        for todo in state['todos'].values():
            todo['executor_contact'] = MANAGER.user_id
        state['todos']['SYNTHETIC-todo-9002']['detail']['isDone'] = True
        save_state(self.state_path, state)
        pairs = dict((task_id, subject)
                     for subject, task_id in self.transport._todo_list_pairs())
        self.assertEqual(pairs['SYNTHETIC-todo-9001'], 'stage:open')
        self.assertEqual(pairs['SYNTHETIC-todo-9002'], 'stage:done')
        statuses = [call['status'] for call in self._todo_list_calls()]
        self.assertEqual(statuses, ['false', 'true'])

    def test_a_todo_completed_after_create_is_still_recovered(self):
        """The todo may be ticked complete before anyone reads it back (#72)."""
        request = self._issue_request()
        self._set_login(MANAGER.user_id)
        first = self._landed_without_receipt(request)
        self.assertEqual(first.outcome, Outcome.UNKNOWN)
        landed = load_state(self.state_path)['todos']
        task_id, todo = next(iter(landed.items()))
        state = load_state(self.state_path)
        state['todos'][task_id]['detail']['isDone'] = True
        state['calls'] = []
        save_state(self.state_path, state)
        again = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(again.outcome, Outcome.VERIFIED)
        self.assertEqual(again.source.resource_id, task_id)
        self.assertEqual(load_state(self.state_path)['todos'][task_id]['subject'],
                         todo['subject'])
        statuses = [call['status'] for call in self._todo_list_calls()]
        self.assertEqual(statuses, ['false', 'true'])

    def test_todo_recovery_never_leans_on_the_list_status_default(self):
        """No scan may omit ``--status``: its live default is undocumented."""
        request = self._issue_request()
        self._set_login(MANAGER.user_id)
        self._unreachable_attempt(request)
        still = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(still.outcome, Outcome.UNKNOWN)
        calls = self._todo_list_calls()
        self.assertTrue(calls)
        self.assertNotIn(None, [call['status'] for call in calls])
        self.assertEqual({call['size'] for call in calls}, {100})

    def test_zero_hits_while_the_actor_is_not_the_login_needs_a_human(self):
        """A blinded scan must not be read as "the todo was never created" (#72)."""
        request = self._issue_request()
        self._set_login('SYNTHETIC-other-login')
        self._unreachable_attempt(request)
        retried = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(retried.outcome, Outcome.UNKNOWN)
        indexed = self._indexed(request.operation_id)
        self.assertTrue(indexed['pending'])
        self.assertEqual(indexed['recovery_state'], RECOVERY_NEEDS_MANUAL)
        self.assertTrue(indexed['needs_manual_confirmation'])
        self.assertEqual(indexed['recovery_reason'], REASON_EXECUTOR_SCOPE)
        self.assertEqual(indexed['recovery_matches'], 0)
        self.assertEqual(indexed['recovery_statuses'], ['false', 'true'])
        self.assertEqual(indexed['recovery_attempts'], 1)
        self.assertEqual(indexed['executor_scope'], SCOPE_OTHER)
        self.assertEqual(indexed['login_user_id'], 'SYNTHETIC-other-login')
        queried = self.transport.exchange(
            'stage.query', {'operation_id': request.operation_id})['result']
        self.assertTrue(queried['needs_manual_confirmation'])
        self.assertEqual(queried['recovery_reason'], REASON_EXECUTOR_SCOPE)
        rows = pending_stage_report(self.work / 'runtime')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['operation_id'], request.operation_id)
        self.assertTrue(rows[0]['needs_manual_confirmation'])
        self.assertEqual(rows[0]['recovery_reason'], REASON_EXECUTOR_SCOPE)
        self.assertEqual(rows[0]['executor_scope'], SCOPE_OTHER)
        self.assertEqual(rows[0]['title'], stage_title(request.loan, request.action))

    def test_zero_hits_with_the_actor_as_login_is_confirmed_not_built(self):
        """The one 0-hit case that may be trusted: actor = login, both statuses."""
        request = self._issue_request()
        self._set_login(MANAGER.user_id)
        self._unreachable_attempt(request)
        retried = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(retried.outcome, Outcome.UNKNOWN)
        indexed = self._indexed(request.operation_id)
        self.assertEqual(indexed['recovery_state'], RECOVERY_NOT_BUILT)
        self.assertFalse(indexed['needs_manual_confirmation'])
        self.assertIsNone(indexed['recovery_reason'])
        self.assertEqual(indexed['executor_scope'], SCOPE_SELF)
        rows = pending_stage_report(self.work / 'runtime')
        self.assertFalse(rows[0]['needs_manual_confirmation'])
        self.assertEqual(rows[0]['recovery_state'], RECOVERY_NOT_BUILT)

    def test_an_unreadable_todo_list_is_manual_not_absent(self):
        """A list we could not read is not a list we read empty (#72)."""
        request = self._issue_request()
        self._set_login(MANAGER.user_id)
        self._unreachable_attempt(request)
        state = load_state(self.state_path)
        state['fail']['todo task list'] = 'SYNTHETIC-throttled'
        save_state(self.state_path, state)
        retried = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(retried.outcome, Outcome.UNKNOWN)
        indexed = self._indexed(request.operation_id)
        self.assertEqual(indexed['recovery_state'], RECOVERY_NEEDS_MANUAL)
        self.assertEqual(indexed['recovery_reason'], REASON_LIST_UNREADABLE)
        self.assertTrue(indexed['needs_manual_confirmation'])
        state = load_state(self.state_path)
        del state['fail']['todo task list']
        save_state(self.state_path, state)
        again = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(again.outcome, Outcome.UNKNOWN)
        recovered = self._indexed(request.operation_id)
        self.assertEqual(recovered['recovery_state'], RECOVERY_NOT_BUILT)
        self.assertEqual(recovered['recovery_attempts'], 2)

    def test_stage_query_by_task_id(self):
        current = loan()
        inventory = stock()
        intent = plan(current, event(Action.APPROVE), inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current, inventory = verify(intent, receipt).loan, receipt.inventory
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        intent = plan(current, reserve, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current = verify(intent, receipt).loan
        issue = StageRequest(stage_operation_id(current, Action.ISSUE),
                             current, Action.ISSUE, MANAGER)
        todo = self.adapter.create_stage(issue, self.binding, self.lease)
        queried = self.transport.exchange('stage.query', {
            'task_id': todo.source.resource_id,
        })
        self.assertEqual(queried['result']['resource_id'], todo.source.resource_id)
        self.assertEqual(queried['result']['container'], TODO_CONTAINER)
        self.assertEqual(queried['result']['kind'], 'todo')


    def _issue_request(self):
        """Loan driven to the point where ISSUE needs its own todo."""
        current = loan()
        inventory = stock()
        intent = plan(current, event(Action.APPROVE), inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current, inventory = verify(intent, receipt).loan, receipt.inventory
        reserve = replace(event(Action.RESERVE), actor=None, evidence_kind='system')
        intent = plan(current, reserve, inventory)
        receipt = self.adapter.submit(intent, self.binding, self.lease)
        current = verify(intent, receipt).loan
        return StageRequest(stage_operation_id(current, Action.ISSUE),
                            current, Action.ISSUE, MANAGER)

    def _seed_todo(self, state, task_id, subject):
        state['todos'][task_id] = {
            'subject': subject,
            'detail': {'taskId': task_id, 'isDone': False, 'finishTime': 0,
                       'executorIds': [9000000199], 'activities': []},
        }

    def _indexed(self, operation_id):
        path = self.work / 'runtime' / 'dws-stage-index.json'
        store = json.loads(path.read_text(encoding='utf-8'))
        return store['by_operation'].get(operation_id)

    def _landed_without_receipt(self, request):
        """First attempt whose write lands live while the envelope never returns."""
        state = load_state(self.state_path)
        state['late_write'] = ['todo task create']
        save_state(self.state_path, state)
        # Observe the login account inside the normal timeout: the annotation
        # written during the tight-timeout attempt must not depend on how fast a
        # subprocess starts under load.
        self.transport._login_user_id()
        self.transport.timeout = 0.3
        try:
            return self.adapter.create_stage(request, self.binding, self.lease)
        finally:
            self.transport.timeout = 30
            state = load_state(self.state_path)
            state['late_write'] = []
            save_state(self.state_path, state)

    def _unreachable_attempt(self, request, seeded=()):
        """First attempt that never reaches the todo space at all."""
        state = load_state(self.state_path)
        for task_id, subject in seeded:
            self._seed_todo(state, task_id, subject)
        state['timeout'] = ['todo task create']
        save_state(self.state_path, state)
        # Same reason as ``_landed_without_receipt``: read the login account at
        # the normal timeout, not inside the 0.3s window.
        self.transport._login_user_id()
        self.transport.timeout = 0.3
        try:
            return self.adapter.create_stage(request, self.binding, self.lease)
        finally:
            self.transport.timeout = 30
            state = load_state(self.state_path)
            state['timeout'] = []
            save_state(self.state_path, state)

    def test_unknown_todo_receipt_is_recovered_by_unique_title(self):
        request = self._issue_request()
        first = self._landed_without_receipt(request)
        self.assertEqual(first.outcome, Outcome.UNKNOWN)
        self.assertEqual(first.creation_evidence, '')
        landed = load_state(self.state_path)['todos']
        self.assertEqual(len(landed), 1)
        task_id, todo = next(iter(landed.items()))
        self.assertEqual(todo['subject'], stage_title(request.loan, request.action))
        indexed = self._indexed(request.operation_id)
        self.assertTrue(indexed['pending'])
        self.assertEqual(indexed['title'], todo['subject'])
        again = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(again.outcome, Outcome.VERIFIED)
        self.assertEqual(verify_stage(request, again), Outcome.VERIFIED)
        self.assertEqual(again.source.resource_id, task_id)
        self.assertEqual(again.creation_evidence, f'todo.task.create:{task_id}')
        self.assertEqual(again.readback_evidence, f'todo.task.get:{task_id}')
        self.assertEqual(len(load_state(self.state_path)['todos']), 1)

    def test_unknown_todo_receipt_without_a_title_match_stays_unknown(self):
        request = self._issue_request()
        first = self._unreachable_attempt(
            request, seeded=[('SYNTHETIC-todo-9001', 'other-stage:subject')])
        self.assertEqual(first.outcome, Outcome.UNKNOWN)
        self.assertTrue(self._indexed(request.operation_id)['pending'])
        retried = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(retried.outcome, Outcome.UNKNOWN)
        self.assertIsNone(retried.source)
        self.assertEqual(retried.creation_evidence, '')
        todos = load_state(self.state_path)['todos']
        self.assertEqual(sorted(todos), ['SYNTHETIC-todo-9001'])

    def test_two_todos_sharing_the_stage_title_stay_unknown(self):
        request = self._issue_request()
        title = stage_title(request.loan, request.action)
        first = self._unreachable_attempt(
            request, seeded=[('SYNTHETIC-todo-9001', title),
                             ('SYNTHETIC-todo-9002', title)])
        self.assertEqual(first.outcome, Outcome.UNKNOWN)
        self.assertTrue(self._indexed(request.operation_id)['pending'])
        retried = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(retried.outcome, Outcome.UNKNOWN)
        self.assertEqual(sorted(load_state(self.state_path)['todos']),
                         ['SYNTHETIC-todo-9001', 'SYNTHETIC-todo-9002'])
        indexed = self._indexed(request.operation_id)
        self.assertEqual(indexed['recovery_matches'], 2)
        self.assertEqual(indexed['recovery_reason'], REASON_TITLE_AMBIGUOUS)
        self.assertTrue(indexed['needs_manual_confirmation'])

    def test_fake_dws_pages_like_the_live_list(self):
        """``--size`` above one page merges without ``hasMore``; at or below it
        admits more pages, which the recovery read must reject."""
        state = load_state(self.state_path)
        self._seed_todo(state, 'SYNTHETIC-todo-9001', 'stage:one')
        self._seed_todo(state, 'SYNTHETIC-todo-9002', 'stage:two')
        save_state(self.state_path, state)
        env = dict(os.environ, FAKE_DWS_STATE=str(self.state_path))

        def list_with(size):
            argv = [sys.executable, str(FAKE_DWS), 'todo', 'task', 'list',
                    '--size', str(size), '--status', 'false', '--format', 'json']
            out = subprocess.run(argv, capture_output=True, text=True, env=env)
            return json.loads(out.stdout)['result']

        small = list_with(1)
        self.assertEqual(len(small['todoCards']), 1)
        self.assertTrue(small['hasMore'])
        merged = list_with(25)
        self.assertEqual(len(merged['todoCards']), 2)
        self.assertNotIn('hasMore', merged)

    def test_todo_list_that_admits_more_pages_is_not_a_match(self):
        request = self._issue_request()
        first = self._landed_without_receipt(request)
        self.assertEqual(first.outcome, Outcome.UNKNOWN)
        self.assertTrue(self._indexed(request.operation_id)['pending'])
        state = load_state(self.state_path)
        state['list_more'] = True
        save_state(self.state_path, state)
        retried = self.adapter.create_stage(request, self.binding, self.lease)
        self.assertEqual(retried.outcome, Outcome.UNKNOWN)
        self.assertEqual(len(load_state(self.state_path)['todos']), 1)


    def _seed_row(self, ref, **changes):
        state = load_state(self.state_path)
        current = replace(loan(), ref=ref, **changes)
        state['records'][f'{ref.container_id}/{ref.resource_id}'] = encode_loan(
            current, self.fields)
        save_state(self.state_path, state)
        return current

    def _borrowed_query(self, borrower):
        return self.transport.exchange('loan.query_borrowed', {
            'tenant_id': LOAN.tenant_id,
            'loan_container': LOAN.container_id,
            'borrower': borrower,
        })

    def test_form_decision_accepts_chinese_agreement(self):
        from integrations.dingtalk.adapter import _form_action

        self.assertEqual(_form_action('同意'), Action.APPROVE)
        self.assertEqual(_form_action('拒绝'), Action.REJECT)
        self.assertEqual(_form_action('agree'), Action.APPROVE)

    def test_loan_query_borrowed_empty_result_is_empty_list(self):
        borrower = encode_loan(loan(), self.fields)[self.fields.borrower][0]['userId']
        payload = self._borrowed_query(borrower)
        self.assertEqual(payload['result']['loan_ids'], [])

    def test_stage_todo_title_is_human_readable(self):
        # 只有 ISSUE / RETURN 阶段会发待办（标题才会被人看到），断这两个真模板。
        from integrations.dingtalk.adapter import stage_title

        current = replace(loan(), state=State.BORROWED)
        for action, marker in ((Action.ISSUE, '待领用确认'), (Action.RETURN, '待归还确认')):
            title = stage_title(current, action)
            self.assertIn(marker, title)
            self.assertIn(current.ref.resource_id, title)
            self.assertNotIn('confirm_issue:', title)
            self.assertNotIn('confirm_return:', title)

    def test_stage_title_renders_due_in_shanghai(self):
        from datetime import timedelta, timezone

        from integrations.dingtalk.adapter import stage_title

        current = loan()
        utc = replace(current, state=State.BORROWED, due_at=current.due_at.astimezone(timezone.utc))
        title = stage_title(utc, Action.REQUEST_RETURN)
        expected = utc.due_at.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
        self.assertIn(expected, title)

    def test_only_todo_stages_carry_a_title(self):
        # 事实：form.create 不带标题（审批/归还请求只有入口行），todo.create 才带。
        form = self.transport._argv('form.create', {
            'operation_id': 'op-1', 'action': 'approve', 'actor': 'someone',
            'tenant_id': LOAN.tenant_id, 'loan_container': LOAN.container_id,
            'loan_id': LOAN.resource_id, 'item_container': ITEM.container_id,
            'item_id': ITEM.resource_id, 'borrower': 'x', 'approver': 'x', 'manager': 'x',
            'config_version': 'c', 'quantity': 1, 'physical_ids': [],
            'title': '【待审批】…',
        })
        self.assertNotIn('--title', form)
        todo = self.transport._argv('todo.create', {
            'action': 'confirm_issue', 'operation_id': 'op-1', 'actor': 'someone',
            'title': '【待领用确认】…',
        })
        self.assertIn('--title', todo)

    def test_todo_create_uses_given_title_and_falls_back(self):
        given = self.transport._argv('todo.create', {
            'action': 'confirm_return', 'operation_id': 'op-1', 'actor': 'someone',
            'title': '【待归还确认】请确认已归还',
        })
        self.assertEqual(given[given.index('--title') + 1], '【待归还确认】请确认已归还')

        legacy = self.transport._argv('todo.create', {
            'action': 'confirm_return', 'operation_id': 'op-1', 'actor': 'someone',
        })
        self.assertEqual(legacy[legacy.index('--title') + 1], 'confirm_return:op-1')

    def test_loan_query_borrowed_returns_only_this_borrower(self):
        mine = self._seed_row(LOAN, state=State.BORROWED)
        borrower = encode_loan(mine, self.fields)[self.fields.borrower][0]['userId']
        other = Resource('record', LOAN.tenant_id, LOAN.container_id, 'recOther')
        self._seed_row(other, state=State.BORROWED,
                       borrower=Identity('contact', LOAN.tenant_id, 'other-user'))
        closed = Resource('record', LOAN.tenant_id, LOAN.container_id, 'recClosed')
        self._seed_row(closed, state=State.CLOSED)
        payload = self._borrowed_query(borrower)
        self.assertEqual(payload['result']['loan_ids'], [LOAN.resource_id])

    def test_loan_query_borrowed_fails_closed_when_truncated(self):
        mine = self._seed_row(LOAN, state=State.BORROWED)
        borrower = encode_loan(mine, self.fields)[self.fields.borrower][0]['userId']
        state = load_state(self.state_path)
        state['query_truncated'] = True
        save_state(self.state_path, state)
        self.blocked(Code.EVIDENCE, lambda: self._borrowed_query(borrower))

    def test_loan_query_borrowed_fails_closed_on_unreadable_row(self):
        mine = self._seed_row(LOAN, state=State.BORROWED)
        borrower = encode_loan(mine, self.fields)[self.fields.borrower][0]['userId']
        state = load_state(self.state_path)
        slot = f'{LOAN.container_id}/{LOAN.resource_id}'
        cells = dict(state['records'][slot])
        cells.pop(self.fields.borrower, None)
        state['records'][slot] = cells
        save_state(self.state_path, state)
        self.blocked(Code.EVIDENCE, lambda: self._borrowed_query(borrower))


class ReturnItemRoutingOverDwsTests(unittest.TestCase):
    """#77: the optional「归还物品」answer, read through the real dws read path.

    The item question is a single-select: the fake stores the option name and the
    read materializes ``{id, name}`` exactly as live aitable does, so this proves
    the adapter matches on the *option name* shape and not on a double-only one.
    """

    OTHER = Resource('record', LOAN.tenant_id, LOAN.container_id, 'recOther')
    OTHER_ITEM = Resource('record', LOAN.tenant_id, 'baseStock/tblStock', 'recItem2')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.state_path = self.work / 'fake-state.json'
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = entry_fields_from(self.fields)
        self.return_form_fields = SYNTHETIC_RETURN_FORM_FIELDS
        kinds = declared_kinds(self.fields, self.entry_fields, self.return_form_fields)
        kinds[self.return_form_fields.item] = SINGLE_SELECT
        state = load_state(self.state_path)
        state['kinds'] = kinds
        save_state(self.state_path, state)
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = DwsTransport(
            [sys.executable, str(FAKE_DWS)], self.fields,
            form_container=FORM_CONTAINER, todo_container=TODO_CONTAINER,
            work_dir=self.work / 'runtime', entry_fields=self.entry_fields,
            extra_env={'FAKE_DWS_STATE': str(self.state_path)},
        )
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            return_form_fields=self.return_form_fields,
            loan_container=LOAN.container_id)

    def tearDown(self):
        self.temp.cleanup()

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def _seed_loan(self, ref, item):
        current = replace(loan(), ref=ref, item=item, state=State.BORROWED)
        state = load_state(self.state_path)
        state['records'][f'{ref.container_id}/{ref.resource_id}'] = encode_loan(
            current, self.fields)
        state['records'][f'{item.container_id}/{item.resource_id}'] = encode_inventory(
            replace(stock(), ref=item), self.fields)
        save_state(self.state_path, state)
        return current

    def _seed_return_row(self, item_name=None, row_id='recReturnRow'):
        cells = {
            self.return_form_fields.borrower: _put_identity(_fixtures.BORROWER),
            self.return_form_fields.occurred_at: _fixtures.NOW.isoformat(),
        }
        if item_name is not None:
            cells[self.return_form_fields.item] = item_name
        state = load_state(self.state_path)
        state['records'][f'{FORM_CONTAINER}/{row_id}'] = cells
        save_state(self.state_path, state)
        return Resource('form', LOAN.tenant_id, FORM_CONTAINER, row_id)

    def _row_cells(self, source):
        payload = self.transport.exchange('record.query', {
            'tenant_id': source.tenant_id,
            'container_id': source.container_id,
            'resource_id': source.resource_id,
        })
        return record_cells(extract_records(payload)[0])

    def test_item_answer_routes_to_the_open_loan_of_that_item(self):
        first = self._seed_loan(LOAN, ITEM)
        second = self._seed_loan(self.OTHER, self.OTHER_ITEM)
        source = self._seed_return_row(self.OTHER_ITEM.resource_id)
        self.assertIsInstance(self._row_cells(source)[self.return_form_fields.item], dict)
        self.assertEqual(
            self.adapter.resolve_return_form_loan(source, first.ref), second.ref)
        self.assertEqual(self.adapter.read_event(second, source).action,
                         Action.REQUEST_RETURN)
        self.blocked(Code.WRONG_LOAN, lambda: self.adapter.read_event(first, source))

    def test_two_open_loans_without_an_item_answer_still_block(self):
        first = self._seed_loan(LOAN, ITEM)
        self._seed_loan(self.OTHER, self.OTHER_ITEM)
        source = self._seed_return_row()
        self.blocked(Code.EVIDENCE,
                     lambda: self.adapter.resolve_return_form_loan(source, first.ref))


if __name__ == '__main__':
    unittest.main()
