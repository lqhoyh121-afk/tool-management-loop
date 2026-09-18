"""Application collection table uses apply_fields, not stage entry_fields (#45)."""
import importlib.util
import json
import sys
import unittest
from datetime import timedelta
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_memory_transport import MemoryTransport

from contracts.flow import accept_application
from contracts.model import Action, Code, ContractError, Resource
from contracts.ports import LedgerScope, RuntimeBinding, StageRequest, stage_operation_id
from integrations.dingtalk.adapter import DingTalkAdapter
from integrations.dingtalk.codec import _put_identity, encode_inventory, encode_loan
from integrations.dingtalk.layout import (
    SYNTHETIC_APPLY_FIELDS, SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_FIELDS)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t45_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t45_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
loan, stock = _fixtures.loan, _fixtures.stock
MANAGER, BORROWER, ITEM, LOAN, NOW = (
    _fixtures.MANAGER, _fixtures.BORROWER, _fixtures.ITEM, _fixtures.LOAN,
    _fixtures.NOW)
SyntheticJournal, SyntheticLease = _synthetic.SyntheticJournal, _synthetic.SyntheticLease

APPLY_CONTAINER = 'synthetic-apply-forms'


def _live_select(name):
    return {'id': f'SYNTHETIC-rand-{name}', 'name': name}


class ApplyFormReadTests(unittest.TestCase):
    def setUp(self):
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = SYNTHETIC_ENTRY_FIELDS
        self.apply_fields = SYNTHETIC_APPLY_FIELDS
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = MemoryTransport(
            self.fields, self.entry_fields, apply_container=APPLY_CONTAINER)
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            apply_fields=self.apply_fields, application_container=APPLY_CONTAINER)
        self.transport.seed_record(loan().ref, encode_loan(loan(), self.fields))
        self.transport.seed_record(stock().ref, encode_inventory(stock(), self.fields))

    def _seed_apply_row(self, form_id='synthetic-apply-row', due=None):
        cells = {
            self.apply_fields.borrower: _put_identity(BORROWER),
            self.apply_fields.quantity: '2',
            self.apply_fields.physical_ids: json.dumps([], ensure_ascii=True),
            self.apply_fields.occurred_at: (NOW - timedelta(days=1)).isoformat(),
            self.apply_fields.due_at: (
                loan().due_at if due is None else due).isoformat(),
        }
        self.transport.seed_record(
            Resource('form', 'synthetic-org', APPLY_CONTAINER, form_id),
            cells,
        )
        return Resource('form', 'synthetic-org', APPLY_CONTAINER, form_id)

    def test_application_row_without_loan_keys_is_trusted_apply(self):
        source = self._seed_apply_row()
        event = self.adapter.read_event(loan(), source)
        self.assertEqual(event.action, Action.APPLY)
        self.assertEqual(event.actor, BORROWER)
        self.assertEqual(event.quantity, loan().quantity)
        # 申请行上的归还时间随事件带出来，受理侧才能比对（#78 第 5 条）。
        self.assertEqual(event.due_at, loan().due_at)
        admitted = accept_application(loan(), event)
        self.assertEqual(admitted.consumed_events, (event.event_id,))

    def test_a_changed_return_time_is_refused_at_acceptance(self):
        """申请提交后改了归还时间：受理可见地跳过（DUE_AT_MISMATCH），不静默按旧时间走。"""
        source = self._seed_apply_row(due=loan().due_at + timedelta(days=3))
        event = self.adapter.read_event(loan(), source)
        self.assertEqual(event.due_at, loan().due_at + timedelta(days=3))
        with self.assertRaises(ContractError) as raised:
            accept_application(loan(), event)
        self.assertEqual(raised.exception.code, Code.DUE)

    def test_an_undeclared_return_time_cell_leaves_the_comparison_off(self):
        """绑定没声明这一格（``unset:``）：事件里 due_at 为空，比对不成立而不是报错。"""
        fields = replace(self.apply_fields, due_at='unset:due_at')
        adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            apply_fields=fields, application_container=APPLY_CONTAINER)
        cells = {
            fields.borrower: _put_identity(BORROWER),
            fields.quantity: '2',
            fields.physical_ids: json.dumps([], ensure_ascii=True),
            fields.occurred_at: (NOW - timedelta(days=1)).isoformat(),
        }
        source = Resource('form', 'synthetic-org', APPLY_CONTAINER, 'synthetic-apply-row-x')
        self.transport.seed_record(source, cells)
        event = adapter.read_event(loan(), source)
        self.assertIsNone(event.due_at)
        self.assertEqual(accept_application(loan(), event).application_evidence,
                         event.evidence_ref)

    def test_entry_row_still_requires_loan_container_and_entry_map(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        self.transport.complete_form(receipt.source.resource_id, 'agree', NOW.isoformat())
        agreed = self.adapter.read_event(loan(), receipt.source)
        self.assertEqual(agreed.action, Action.APPROVE)

    def test_request_return_decision_alias(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        key = ('synthetic-org', 'synthetic-forms', receipt.source.resource_id)
        cells = self.transport.records[key]
        cells[self.entry_fields.decision] = _live_select('request_return')
        cells[self.entry_fields.occurred_at] = NOW.isoformat()
        cells[self.entry_fields.return_container] = 'synthetic-returns'
        cells[self.entry_fields.return_id] = 'synthetic-return'
        event = self.adapter.read_event(loan(), receipt.source)
        self.assertEqual(event.action, Action.REQUEST_RETURN)

    def test_entry_action_field_when_decision_missing(self):
        request = StageRequest(stage_operation_id(loan(), Action.APPROVE),
                               loan(), Action.APPROVE, MANAGER)
        receipt = self.adapter.create_stage(request, self.binding, self.lease)
        key = ('synthetic-org', 'synthetic-forms', receipt.source.resource_id)
        cells = self.transport.records[key]
        cells.pop(self.entry_fields.decision, None)
        cells[self.entry_fields.action] = 'request_return'
        cells[self.entry_fields.occurred_at] = NOW.isoformat()
        cells[self.entry_fields.return_container] = 'synthetic-returns'
        cells[self.entry_fields.return_id] = 'synthetic-return'
        event = self.adapter.read_event(loan(), receipt.source)
        self.assertEqual(event.action, Action.REQUEST_RETURN)


if __name__ == '__main__':
    unittest.main()
