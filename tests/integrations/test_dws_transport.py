"""Isolation tests for DwsTransport. Fake subprocess only; no live DingTalk."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t031_fake_dws import load_state, save_state

from contracts.flow import plan, verify
from contracts.model import Action, Code, ContractError, Outcome, Resource, State
from contracts.ports import LedgerScope, RuntimeBinding, StageRequest, stage_operation_id
from integrations.dingtalk.adapter import DingTalkAdapter
from integrations.dingtalk.codec import encode_inventory, encode_loan
from integrations.dingtalk.dws_transport import (DwsTransport, split_container,
                                                 windows_native_path)
from integrations.dingtalk.errors import UnsupportedShapeError
from integrations.dingtalk.layout import SYNTHETIC_FIELDS, entry_fields_from


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
        save_state(self.state_path, load_state(self.state_path))
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = entry_fields_from(self.fields)
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


if __name__ == '__main__':
    unittest.main()
