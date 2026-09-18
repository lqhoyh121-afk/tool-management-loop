"""Return form rows: borrower + return time, plus the optional「归还物品」cell (#49, #77)."""
import importlib.util
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from t03_memory_transport import MemoryTransport

from contracts.flow import plan, verify
from contracts.model import Action, Code, ContractError, Outcome, Resource, State
from contracts.ports import LedgerScope, RuntimeBinding
from integrations.dingtalk.adapter import DingTalkAdapter, TitleDisplay
from integrations.dingtalk.codec import _put_identity, encode_inventory, encode_loan
from integrations.dingtalk.layout import (
    SYNTHETIC_ENTRY_FIELDS, SYNTHETIC_FIELDS, SYNTHETIC_RETURN_FORM_FIELDS,
    ReturnFormFieldMap)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROOT = Path(__file__).resolve().parents[2]
_fixtures = _load('t49_port_fixtures', _ROOT / 'tests' / 'contracts' / 'fixtures.py')
_synthetic = _load('t49_port_synthetic', _ROOT / 'tests' / 'contracts' / 'synthetic.py')
loan, stock = _fixtures.loan, _fixtures.stock
MANAGER, BORROWER, ITEM, LOAN, NOW = (
    _fixtures.MANAGER, _fixtures.BORROWER, _fixtures.ITEM, _fixtures.LOAN, _fixtures.NOW)
SyntheticJournal, SyntheticLease = _synthetic.SyntheticJournal, _synthetic.SyntheticLease

ENTRY_CONTAINER = 'synthetic-forms'
LOAN_CONTAINER = 'synthetic-loans'
ITEM2 = Resource('record', 'synthetic-org', 'synthetic-stock', 'synthetic-item-2')
# 库存表里的物品名称列（#76 的 `title_display.item_name_field`）与物品的名字。
ITEM_NAME_FIELD = 'fldSYN-item-name'
ITEM_NAME = 'SYN-万用表'


def _live_select(name):
    return {'id': f'SYNTHETIC-rand-{name}', 'name': name}


def _borrowed_loan():
    current = replace(loan(), state=State.BORROWED)
    cells = encode_loan(current, SYNTHETIC_FIELDS)
    cells[SYNTHETIC_FIELDS.state] = _live_select(State.BORROWED.value)
    cells[SYNTHETIC_FIELDS.tracked] = _live_select('false')
    return current, cells


class ReturnFormReadTests(unittest.TestCase):
    def setUp(self):
        self.fields = SYNTHETIC_FIELDS
        self.entry_fields = SYNTHETIC_ENTRY_FIELDS
        self.return_form_fields = SYNTHETIC_RETURN_FORM_FIELDS
        self.journal = SyntheticJournal()
        self.leases = SyntheticLease()
        self.lease = self.leases.acquire(LedgerScope.from_record(ITEM), MANAGER)
        self.binding = RuntimeBinding(
            MANAGER, LedgerScope.from_record(ITEM), 'synthetic-config-v1',
            MANAGER, MANAGER, 'synthetic-readback', True, True, True, True)
        self.transport = MemoryTransport(self.fields, self.entry_fields)
        self.adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            return_form_fields=self.return_form_fields,
            entry_container=ENTRY_CONTAINER, loan_container=LOAN_CONTAINER)
        borrowed, cells = _borrowed_loan()
        self.borrowed = borrowed
        self.transport.seed_record(borrowed.ref, cells)
        self.transport.seed_record(stock().ref, encode_inventory(stock(), self.fields))

    def blocked(self, code, fn):
        with self.assertRaises(ContractError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def _seed_return_row(self, form_id='synthetic-return-form-row', item=None):
        cells = {
            self.return_form_fields.borrower: _put_identity(BORROWER),
            self.return_form_fields.occurred_at: NOW.isoformat(),
        }
        if item is not None:
            cells[self.return_form_fields.item] = item
        source = Resource('form', 'synthetic-org', ENTRY_CONTAINER, form_id)
        self.transport.seed_record(source, cells)
        return source

    def _second_borrowed(self, resource_id='synthetic-loan-2', item=None):
        """A second open loan for the same borrower, of ``item`` (default: another item)."""
        current = replace(self.borrowed, ref=replace(
            self.borrowed.ref, resource_id=resource_id), item=item or ITEM2)
        cells = encode_loan(current, self.fields)
        cells[self.fields.state] = _live_select(State.BORROWED.value)
        cells[self.fields.tracked] = _live_select('false')
        self.transport.seed_record(current.ref, cells)
        self.transport.seed_record(current.item, encode_inventory(stock(), self.fields))
        return current

    def test_minimal_return_form_row_reads_request_return(self):
        source = self._seed_return_row()
        event = self.adapter.read_event(self.borrowed, source)
        self.assertEqual(event.action, Action.REQUEST_RETURN)
        self.assertEqual(event.actor, BORROWER)
        self.assertEqual(event.return_ref, Resource(
            'record', 'synthetic-org', ENTRY_CONTAINER, source.resource_id))
        intent = plan(self.borrowed, event, stock())
        self.assertEqual(intent.after.state, State.AWAITING_RETURN)

    def test_resolve_return_form_loan_ignores_wrong_hint(self):
        source = self._seed_return_row()
        wrong = replace(self.borrowed.ref, resource_id='synthetic-other-loan')
        resolved = self.adapter.resolve_return_form_loan(source, wrong)
        self.assertEqual(resolved, self.borrowed.ref)

    def test_two_borrowed_loans_for_same_borrower_blocks(self):
        self._second_borrowed('synthetic-loan-2', ITEM)
        source = self._seed_return_row()
        self.blocked(Code.EVIDENCE, lambda: self.adapter.read_event(self.borrowed, source))
        self.blocked(Code.EVIDENCE,
                     lambda: self.adapter.resolve_return_form_loan(source, self.borrowed.ref))

    def test_item_answer_routes_to_the_loan_of_that_item(self):
        """(b) two open loans + the row names the item → that loan, not the other."""
        second = self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row(item=ITEM2.resource_id)
        self.assertEqual(
            self.adapter.resolve_return_form_loan(source, self.borrowed.ref), second.ref)
        event = self.adapter.read_event(second, source)
        self.assertEqual(event.action, Action.REQUEST_RETURN)
        self.assertEqual(event.actor, BORROWER)
        self.assertEqual(event.return_ref, Resource(
            'record', 'synthetic-org', ENTRY_CONTAINER, source.resource_id))
        intent = plan(second, event, self.adapter.read_inventory(ITEM2))
        self.assertEqual(intent.after.state, State.AWAITING_RETURN)

    def test_item_answer_routes_the_first_loan_too(self):
        second = self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row(item=ITEM.resource_id)
        self.assertEqual(
            self.adapter.resolve_return_form_loan(source, second.ref), self.borrowed.ref)
        event = self.adapter.read_event(self.borrowed, source)
        self.assertEqual(event.action, Action.REQUEST_RETURN)

    def test_item_answer_may_come_back_as_a_single_select_name(self):
        second = self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row(item=_live_select(ITEM2.resource_id))
        self.assertEqual(
            self.adapter.resolve_return_form_loan(source, self.borrowed.ref), second.ref)
        self.assertEqual(self.adapter.read_event(second, source).action, Action.REQUEST_RETURN)

    def test_item_answer_naming_the_other_loan_is_wrong_loan(self):
        self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row(item=ITEM2.resource_id)
        self.blocked(Code.WRONG_LOAN, lambda: self.adapter.read_event(self.borrowed, source))

    def test_item_answer_that_matches_no_open_loan_blocks(self):
        self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row(item='synthetic-item-unknown')
        self.blocked(Code.EVIDENCE,
                     lambda: self.adapter.resolve_return_form_loan(source, self.borrowed.ref))

    def test_item_answer_cannot_break_a_tie_of_the_same_item(self):
        # 同一物品借了多件：给了物品也还是 ≥2 张，继续 fail-closed，不许挑一张。
        self._second_borrowed('synthetic-loan-2', ITEM)
        source = self._seed_return_row(item=ITEM.resource_id)
        self.blocked(Code.EVIDENCE,
                     lambda: self.adapter.resolve_return_form_loan(source, self.borrowed.ref))
        self.blocked(Code.EVIDENCE, lambda: self.adapter.read_event(self.borrowed, source))

    def test_unreadable_item_answer_blocks_rather_than_matching(self):
        self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row(item=12345)
        self.blocked(Code.EVIDENCE,
                     lambda: self.adapter.resolve_return_form_loan(source, self.borrowed.ref))

    def test_item_answer_with_one_open_loan_still_routes(self):
        source = self._seed_return_row(item=ITEM.resource_id)
        resolved = self.adapter.resolve_return_form_loan(source, self.borrowed.ref)
        self.assertEqual(resolved, self.borrowed.ref)
        self.assertEqual(self.adapter.read_event(self.borrowed, source).action,
                         Action.REQUEST_RETURN)

    def test_binding_without_the_item_key_behaves_as_before(self):
        """(d) 绑定里没有这一格：一切同 #49（只给借用人，1 张定性，2 张拦下）。"""
        without_item = ReturnFormFieldMap(
            borrower=self.return_form_fields.borrower,
            occurred_at=self.return_form_fields.occurred_at)
        self.assertEqual(without_item.item, '')
        adapter = DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            return_form_fields=without_item,
            entry_container=ENTRY_CONTAINER, loan_container=LOAN_CONTAINER)
        single = self._seed_return_row(item=ITEM.resource_id)
        self.assertEqual(adapter.resolve_return_form_loan(single, self.borrowed.ref),
                         self.borrowed.ref)
        self.assertEqual(adapter.read_event(self.borrowed, single).action,
                         Action.REQUEST_RETURN)
        self._second_borrowed('synthetic-loan-2', ITEM2)
        both = self._seed_return_row('synthetic-return-form-row-2', item=ITEM2.resource_id)
        self.blocked(Code.EVIDENCE,
                     lambda: adapter.resolve_return_form_loan(both, self.borrowed.ref))

    def _named_adapter(self):
        """同一个世界，但绑定点名了「物品名称那一列」（#76 的 title_display）。

        只有点了名，adapter 才会去读物品名 —— 未绑定 = 名称读不到 = 名称这条路走不通。
        """
        return DingTalkAdapter(
            self.transport, self.journal, self.leases, self.fields, self.entry_fields,
            return_form_fields=self.return_form_fields,
            entry_container=ENTRY_CONTAINER, loan_container=LOAN_CONTAINER,
            title_display=TitleDisplay(ITEM_NAME_FIELD))

    def _name_lookups(self):
        return [arguments for command, arguments in self.transport.calls
                if command == 'title.names']

    def test_item_name_answer_routes_to_that_items_only_open_loan(self):
        """(a) 答案是物品**名称**（钉钉那格的选项名）且恰好 1 张 → 定性到那张。"""
        adapter = self._named_adapter()
        self.transport.title_names = {'item_name': ITEM_NAME}
        source = self._seed_return_row(item=_live_select(ITEM_NAME))
        self.assertEqual(adapter.resolve_return_form_loan(source, self.borrowed.ref),
                         self.borrowed.ref)
        lookups = self._name_lookups()
        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0]['item_id'], self.borrowed.item.resource_id)
        self.assertEqual(lookups[0]['item_name_field'], ITEM_NAME_FIELD)
        self.assertIs(lookups[0]['borrower_names'], False)  # 定性不为标题去查通讯录
        event = adapter.read_event(self.borrowed, source)
        self.assertEqual(event.action, Action.REQUEST_RETURN)
        self.assertEqual(event.actor, BORROWER)
        self.assertEqual(event.return_ref, Resource(
            'record', 'synthetic-org', ENTRY_CONTAINER, source.resource_id))

    def test_item_name_answer_may_come_back_as_plain_text(self):
        """名称那格读回来是纯文本（未 materialize）也认。"""
        adapter = self._named_adapter()
        self.transport.title_names = {'item_name': ITEM_NAME}
        source = self._seed_return_row(item=ITEM_NAME)
        self.assertEqual(adapter.resolve_return_form_loan(source, self.borrowed.ref),
                         self.borrowed.ref)

    def test_item_name_answer_without_a_readable_name_blocks(self):
        """(b) 名称给了但读不到（回包成功、没有名字）→ EVIDENCE，不是「不匹配」。

        一张在借单下面「按借用人唯一」本来是成立的 —— 这里必须拦下：物品那格是真人
        用来消歧的，读不到名字时把它丢掉就等于替人猜。
        """
        adapter = self._named_adapter()
        source = self._seed_return_row(item=_live_select(ITEM_NAME))
        self.blocked(Code.EVIDENCE,
                     lambda: adapter.resolve_return_form_loan(source, self.borrowed.ref))
        self.blocked(Code.EVIDENCE, lambda: adapter.read_event(self.borrowed, source))

    def test_item_name_answer_with_a_broken_name_lookup_blocks(self):
        """(b) 名称这一次读报错 / 没回包 → 同上：拦下。"""
        adapter = self._named_adapter()
        source = self._seed_return_row(item=_live_select(ITEM_NAME))
        for arm in ({'title_names_code': 'PERMISSION_DENIED'},
                    {'drop_once': ['title.names']}):
            for key, value in arm.items():
                setattr(self.transport, key, value)
            self.blocked(Code.EVIDENCE, lambda: adapter.resolve_return_form_loan(
                source, self.borrowed.ref))
            self.transport.title_names_code = None
            self.transport.drop_once = []

    def test_item_name_answer_without_the_name_column_blocks(self):
        """(e) 只绑了「归还物品」、没绑物品名称列：答案是人能填的名字 → 拦下。

        这里**不许**退回成「按借用人唯一」（一张单会被默默定性），也不许变成别的
        口径 —— 与 #77 一样是 EVIDENCE，操作员把名称列绑上就能定性。
        """
        source = self._seed_return_row(item=_live_select(ITEM_NAME))
        self.blocked(Code.EVIDENCE,
                     lambda: self.adapter.resolve_return_form_loan(source, self.borrowed.ref))
        self._second_borrowed('synthetic-loan-2', ITEM2)
        self.blocked(Code.EVIDENCE, lambda: self.adapter.read_event(self.borrowed, source))

    def test_two_open_loans_whose_items_share_the_answered_name_block(self):
        """(d) 两个物品读出来同名、答案是这个名字 → 两张单，继续 fail-closed。"""
        adapter = self._named_adapter()
        self.transport.title_names = {'item_name': ITEM_NAME}
        self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row(item=_live_select(ITEM_NAME))
        self.blocked(Code.EVIDENCE,
                     lambda: adapter.resolve_return_form_loan(source, self.borrowed.ref))
        self.blocked(Code.EVIDENCE, lambda: adapter.read_event(self.borrowed, source))

    def test_record_id_answer_decides_without_asking_for_any_name(self):
        """(c) #77 的记录 ID 匹配一字不变：命中就直接定性，不查名称。"""
        adapter = self._named_adapter()
        self.transport.title_names = {'item_name': ITEM_NAME}
        second = self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row(item=_live_select(ITEM2.resource_id))
        self.assertEqual(adapter.resolve_return_form_loan(source, self.borrowed.ref),
                         second.ref)
        self.assertEqual(adapter.read_event(second, source).action, Action.REQUEST_RETURN)
        self.assertEqual(self._name_lookups(), [])

    def test_record_id_answer_survives_a_broken_name_lookup(self):
        """(c) 不得退化：名称这条路坏掉时，记录 ID 这条照旧定性。"""
        adapter = self._named_adapter()
        self.transport.title_names_code = 'PERMISSION_DENIED'
        second = self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row(item=_live_select(ITEM2.resource_id))
        self.assertEqual(adapter.resolve_return_form_loan(source, self.borrowed.ref),
                         second.ref)
        self.assertEqual(adapter.read_event(second, source).action, Action.REQUEST_RETURN)
        self.assertEqual(self._name_lookups(), [])

    def test_record_id_answer_cannot_break_a_tie_of_the_same_item(self):
        """(c) 同一物品两张单 + 答案是记录 ID → 还是 2 张，拦下。"""
        adapter = self._named_adapter()
        self.transport.title_names = {'item_name': ITEM_NAME}
        self._second_borrowed('synthetic-loan-2', ITEM)
        source = self._seed_return_row(item=_live_select(ITEM.resource_id))
        self.blocked(Code.EVIDENCE,
                     lambda: adapter.resolve_return_form_loan(source, self.borrowed.ref))

    def test_empty_item_answer_with_the_name_column_bound_keeps_the_old_path(self):
        """(c) 没填「归还物品」时与 #49 一字不差，名称这条路根本不该被走。"""
        adapter = self._named_adapter()
        self.transport.title_names = {'item_name': ITEM_NAME}
        source = self._seed_return_row()
        self.assertEqual(adapter.resolve_return_form_loan(source, self.borrowed.ref),
                         self.borrowed.ref)
        self.assertEqual(adapter.read_event(self.borrowed, source).action,
                         Action.REQUEST_RETURN)
        self.assertEqual(self._name_lookups(), [])

    def test_empty_item_answer_with_two_open_loans_still_blocks(self):
        """(c) 没填答案 + 两张单 → 与 #49 一样拦下。"""
        adapter = self._named_adapter()
        self.transport.title_names = {'item_name': ITEM_NAME}
        self._second_borrowed('synthetic-loan-2', ITEM2)
        source = self._seed_return_row()
        self.blocked(Code.EVIDENCE,
                     lambda: adapter.resolve_return_form_loan(source, self.borrowed.ref))

    def test_engine_precreated_row_still_uses_entry_map(self):
        cells = {
            self.entry_fields.loan_container: LOAN.container_id,
            self.entry_fields.loan_id: LOAN.resource_id,
            self.entry_fields.config_version: self.borrowed.config_version,
            self.entry_fields.decision: _live_select('return'),
            self.entry_fields.borrower: _put_identity(BORROWER),
            self.entry_fields.approver: _put_identity(MANAGER),
            self.entry_fields.manager: _put_identity(MANAGER),
            self.entry_fields.quantity: '2',
            self.entry_fields.physical_ids: '[]',
            self.entry_fields.occurred_at: NOW.isoformat(),
            self.entry_fields.return_container: 'synthetic-returns',
            self.entry_fields.return_id: 'synthetic-return-record',
            self.entry_fields.action: 'request_return',
            self.entry_fields.operation_id: 'synthetic-op',
        }
        source = Resource('form', 'synthetic-org', ENTRY_CONTAINER, 'synthetic-engine-row')
        self.transport.seed_record(source, cells)
        event = self.adapter.read_event(self.borrowed, source)
        self.assertEqual(event.action, Action.REQUEST_RETURN)
        self.assertEqual(event.return_ref.resource_id, 'synthetic-return-record')


if __name__ == '__main__':
    unittest.main()
