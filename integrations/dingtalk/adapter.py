"""DingTalk Read/Write/Stage ports over an injected transport.

No credentials, no real platform calls. Tests inject a fake transport.
Unknown write results stay unknown until an exact query. A query that
finds both loan and stock still at the pre-write snapshot is NOT_SENT and
may be retried. Partial writes stay unknown and are not replayed.

Approval truth has exactly one place: the stage entry row's decision column
(``_read_entry_form_event``). The approver's todo only carries the stage to
the person — a completed todo is never an approval conclusion.
"""
from dataclasses import dataclass, replace

from contracts.flow import Receipt, verify
from datetime import timedelta, timezone

from contracts.model import (Action, Code, ContractError, Event, Identity,
                             IdentityBinding, Outcome, Resource, State, require, text)
from contracts.ports import StageReceipt, StageRequest, check_binding, verify_stage

from .application import ApplicationDraft, application_marker
from .cells import (MissingFieldError, read_datetime, read_single_select,
                    read_text)
from .codec import (_identity, _int_count, _text_list, decode_inventory,
                    decode_loan, encode_inventory, encode_loan)
from .envelope import (created_record_id, extract_records, query_rows,
                       record_cells, record_id)
from .errors import (BusinessErrorResponse, DingTalkShapeError,
                     UnknownResultError)
from .identity import TODO
from .todo import (completion_at, completion_events, executor_refs, finish_time,
                   read_todo_detail)
from .transport import require_envelope, require_todo_envelope


def _closed(exc, code=Code.EVIDENCE):
    raise ContractError(code) from exc


def _is_declared(field_id):
    """这一格在这份绑定里声明过没有（不是 ``unset:`` 哨兵、不是空串）。

    ``unset:<key>`` 是绑定表达「本实例没有这个问题」（与申请读侧的逐件编号同约定），
    它不是字段 ID，绝不能被当字段 ID 去读。可选格（如申请行的归还时间）用它区分
    「没声明这一格」与「这一格读到了空值」。
    """
    return (isinstance(field_id, str) and bool(field_id.strip())
            and not field_id.startswith('unset:'))


def _business_code(exc):
    if exc.code in ('FORBIDDEN', 'PERMISSION_DENIED'):
        return Code.IDENTITY
    return Code.UNKNOWN


_FORM_DECISIONS = {
    'apply': Action.APPLY,
    'agree': Action.APPROVE,
    '同意': Action.APPROVE,
    'reject': Action.REJECT,
    '拒绝': Action.REJECT,
    'cancel': Action.CANCEL,
    'return': Action.REQUEST_RETURN,
    'request_return': Action.REQUEST_RETURN,
    '归还': Action.REQUEST_RETURN,
}


def _form_action(name):
    try:
        return _FORM_DECISIONS[name]
    except KeyError as exc:
        raise ContractError(Code.EVIDENCE) from exc



# 标题是执行人在待办列表里唯一的信息来源：除了单号，还要能看出**谁借的、借的什么、
# 什么时候到期**（issue #76 真机体验缺口）。`{item}` / `{borrower}` 优先用显示名，查不到
# 就退回资源 id / userId（见 `stage_title` 与 `DisplayNames`）；`{entry}` 是审批入口链接，
# 只有绑定显式给了才有内容（见 `_entry_suffix`）。
_STAGE_TITLES = {
    Action.APPROVE: ('【待审批】请审批借出 {item} ×{quantity} ｜ 借用人 {borrower}'
                     ' ｜ 到期 {due} ｜ 单号 {loan_id} ｜ 填：决定 + 发生时间{entry}'),
    Action.ISSUE: ('【待领用确认】请确认已领用 {item} ×{quantity} ｜ 借用人 {borrower}'
                   ' ｜ 到期 {due} ｜ 单号 {loan_id}'),
    Action.REQUEST_RETURN: '【待归还】到期 {due} ｜ 单号 {loan_id} ｜ 归还后请填归还表',
    Action.RETURN: ('【待归还确认】请确认已归还 {item} ×{quantity} ｜ 借用人 {borrower}'
                    ' ｜ 到期 {due} ｜ 单号 {loan_id} ｜ 填：决定 + 发生时间'),
}

# 审批阶段另发一条催办待办给审批人。它的成败不进回执：审批结论的唯一真源是入口行的
# 「决定」列，待办点没点完成都不能把结论读出来或改掉（只有 ISSUE/RETURN 以待办完成
# 作为阶段证据，见 `_read_todo_event`）。
_APPROVER_TODO_ACTIONS = (Action.APPROVE,)


@dataclass(frozen=True)
class TitleDisplay:
    """绑定里可选的一段「把标题写成人话」配置。三个键都可缺。

    真实字段 ID / 表 ID / 表单链接只放本机绑定，不进仓库。缺一个键就按下面的口径退回，
    绝不因为配置不全而让阶段建不出来：

    - ``item_name_field``：库存表里物品名称那一列的字段 ID；没给就用物品记录 ID。
    - ``borrower_names``：是否查通讯录显示名；查不到就用 userId。
    - ``approve_entry_url``：审批收集表的分享链接；没给就不拼链接（保持老标题）。
    """
    item_name_field: str = ''
    borrower_names: bool = False
    approve_entry_url: str = ''

    def __post_init__(self):
        require(isinstance(self.item_name_field, str))
        require(isinstance(self.approve_entry_url, str))
        require(type(self.borrower_names) is bool)

    @property
    def names_enabled(self):
        """要不要为标题多查一次名：只有配置真的要求了才查。"""
        return bool(self.item_name_field.strip()) or self.borrower_names


@dataclass(frozen=True)
class DisplayNames:
    """标题里替换 id 的显示串。空串 = 没查到，调用方退回 id。"""
    item: str = ''
    borrower: str = ''


def _entry_suffix(url):
    """审批待办里那句「去哪儿填」；没配置链接就什么都不加。"""
    if not isinstance(url, str) or not url.strip():
        return ''
    return f' ｜ 填表→ {url.strip()}'


def _display_names_from(payload):
    """``title.names`` 报文里的显示名；形状不认就当成没查到（退回 id）。"""
    if not isinstance(payload, dict) or payload.get('status') != 'success':
        return DisplayNames()
    result = payload.get('result')
    if not isinstance(result, dict):
        return DisplayNames()
    item = result.get('item_name')
    borrower = result.get('borrower_name')
    return DisplayNames(item if isinstance(item, str) else '',
                        borrower if isinstance(borrower, str) else '')


def stage_title(loan, action, display=None, entry_url=''):
    """Human-readable stage todo title.

    The executor reads this in a todo list: keep the business单号 and what to do,
    and leave the internal operation id out of the title (it stays in the local
    stage index). Unknown actions fall back to the business单号 only.

    ``display`` carries the resolved 借用人 / 物品 names and ``entry_url`` the
    approval-collection link. Both are optional and purely cosmetic: a missing
    or failed lookup degrades to the raw resource id / userId and to the
    link-less title instead of blocking the stage.

    这个字符串同时也是超时回查的匹配键（transport 把发出去的那份记在阶段索引里），
    所以它必须只由「这笔单 + 这个动作 + 当次查到的显示名」决定，不能掺时间戳。
    """
    template = _STAGE_TITLES.get(action)
    shanghai = timezone(timedelta(hours=8))
    names = display if isinstance(display, DisplayNames) else DisplayNames()
    if template is None:
        return f'{action.value} ｜ 单号 {loan.ref.resource_id}'
    return template.format(
        quantity=loan.quantity,
        loan_id=loan.ref.resource_id,
        due=loan.due_at.astimezone(shanghai).strftime('%Y-%m-%d %H:%M'),
        item=names.item.strip() or loan.item.resource_id,
        borrower=names.borrower.strip() or loan.borrower.user_id,
        entry=_entry_suffix(entry_url))


class DingTalkAdapter:
    """Implements ReadPort, WritePort and StagePort against `transport`."""

    def __init__(self, transport, journal, leases, fields, entry_fields,
                 apply_fields=None, application_container=None,
                 return_form_fields=None, entry_container=None, loan_container=None,
                 title_display=None):
        self.transport = transport
        self.journal = journal
        self.leases = leases
        self.fields = fields
        self.entry_fields = entry_fields
        self.apply_fields = apply_fields
        self.application_container = application_container or ''
        self.return_form_fields = return_form_fields
        self.entry_container = entry_container or ''
        self.loan_container = loan_container or ''
        self.title_display = title_display

    def read_loan(self, ref):
        return decode_loan(ref, self._record_cells(ref), self.fields)

    def read_inventory(self, ref):
        return decode_inventory(ref, self._record_cells(ref), self.fields)

    def read_event(self, loan, source):
        if source.kind == 'form':
            return self._read_form_event(loan, source)
        if source.kind == 'todo':
            return self._read_todo_event(loan, source)
        raise ContractError(Code.EVIDENCE)

    def submit(self, intent, binding, lease):
        self.leases.assert_held(lease)
        check_binding(binding, intent.before)
        self.journal.prepare(intent)
        _, saved = self.journal.load(intent.operation_id)
        if saved is not None:
            if saved.outcome == Outcome.VERIFIED:
                return saved
            if saved.outcome in (Outcome.UNKNOWN, Outcome.NOT_SENT):
                queried = self.query(intent)
                if queried.outcome == Outcome.VERIFIED:
                    return queried
                if queried.outcome != Outcome.NOT_SENT:
                    raise ContractError(Code.UNKNOWN)
        current = self.read_loan(intent.before.ref)
        stock = self.read_inventory(intent.stock_before.ref)
        require(current == intent.before, Code.CONFLICT)
        require(stock == intent.stock_before, Code.CONFLICT)
        self.journal.save_receipt(Receipt(intent.operation_id, Outcome.UNKNOWN))
        try:
            self._put_record(intent.after.ref, encode_loan(intent.after, self.fields))
            self._put_record(intent.stock_after.ref, encode_inventory(intent.stock_after, self.fields))
        except UnknownResultError:
            return Receipt(intent.operation_id, Outcome.UNKNOWN)
        except BusinessErrorResponse as exc:
            if _business_code(exc) is Code.IDENTITY:
                raise ContractError(Code.IDENTITY) from exc
            return Receipt(intent.operation_id, Outcome.UNKNOWN)
        return self.query(intent)

    def query(self, intent):
        try:
            loan = self.read_loan(intent.after.ref)
            inventory = self.read_inventory(intent.stock_after.ref)
        except (UnknownResultError, ContractError):
            receipt = Receipt(intent.operation_id, Outcome.UNKNOWN)
            self.journal.save_receipt(receipt)
            return receipt
        evidence = (
            f'read:{intent.after.ref.resource_id};'
            f'read:{intent.stock_after.ref.resource_id}'
        )
        receipt = Receipt(intent.operation_id, Outcome.VERIFIED, evidence, loan, inventory)
        checked = verify(intent, receipt).outcome
        if checked != Outcome.VERIFIED:
            if loan == intent.before and inventory == intent.stock_before:
                receipt = Receipt(intent.operation_id, Outcome.NOT_SENT, evidence, loan, inventory)
            else:
                receipt = Receipt(intent.operation_id, Outcome.UNKNOWN, evidence, loan, inventory)
        self.journal.save_receipt(receipt)
        return receipt

    def create_stage(self, request: StageRequest, binding, lease: str) -> StageReceipt:
        self.leases.assert_held(lease)
        check_binding(binding, request.loan)
        self.journal.prepare(request)
        _, saved = self.journal.load(request.operation_id)
        if saved is not None:
            if saved.outcome == Outcome.VERIFIED:
                return saved
            if saved.outcome == Outcome.UNKNOWN:
                return self.query_stage(request)
        self.journal.save_receipt(StageReceipt(request.operation_id, Outcome.UNKNOWN))
        command = 'todo.create' if request.action in (Action.ISSUE, Action.RETURN) else 'form.create'
        display = self._display_names(request.loan)
        try:
            payload = self.transport.exchange(command, self._stage_arguments(
                request, request.operation_id, request.actor.user_id, display))
            if command == 'todo.create':
                require_todo_envelope(payload)
            else:
                require_envelope(payload)
        except UnknownResultError:
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
            self.journal.save_receipt(receipt)
            return receipt
        except BusinessErrorResponse as exc:
            if _business_code(exc) is Code.IDENTITY:
                raise ContractError(Code.IDENTITY) from exc
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
            self.journal.save_receipt(receipt)
            return receipt
        except DingTalkShapeError:
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
            self.journal.save_receipt(receipt)
            return receipt
        if request.action in _APPROVER_TODO_ACTIONS:
            self._nudge_approver(request, display)
        return self.query_stage(request)

    def _display_names(self, loan):
        """借用人 / 物品的显示名；只有绑定点名要了才查，查不到一律退回 id。

        这是纯装饰性的一次读，进不了任何回执：解析不出名字不改变阶段结果，也不改变
        审批结论（结论只在入口行的「决定」列）。传输层把可用/失败都收成一个报文，
        这里只做宽进严出的取值。
        """
        config = self.title_display
        if config is None or not config.names_enabled:
            return DisplayNames()
        try:
            payload = self.transport.exchange('title.names', {
                'tenant_id': loan.ref.tenant_id,
                'item_container': loan.item.container_id,
                'item_id': loan.item.resource_id,
                'borrower': loan.borrower.user_id,
                'item_name_field': config.item_name_field.strip(),
                'borrower_names': config.borrower_names,
            })
        except (UnknownResultError, DingTalkShapeError, ContractError,
                KeyError, TypeError, ValueError):
            return DisplayNames()
        return _display_names_from(payload)

    def _stage_arguments(self, request: StageRequest, operation_id, actor,
                         display=None):
        """One stage payload; operation id and recipient stay the caller's choice.

        The approver nudge reuses this shape under its own operation id, so it can
        never take over the stage-index entry the entry row owns.
        """
        return {
            'operation_id': operation_id,
            'tenant_id': request.loan.ref.tenant_id,
            'loan_container': request.loan.ref.container_id,
            'loan_id': request.loan.ref.resource_id,
            'item_container': request.loan.item.container_id,
            'item_id': request.loan.item.resource_id,
            'action': request.action.value,
            'title': stage_title(request.loan, request.action, display,
                                 self._approve_entry_url()),
            'actor': actor,
            'borrower': request.loan.borrower.user_id,
            'approver': request.loan.approver.user_id,
            'manager': request.loan.manager.user_id,
            'config_version': request.loan.config_version,
            'quantity': request.loan.quantity,
            'physical_ids': list(request.loan.physical_ids),
        }

    def _approve_entry_url(self):
        """审批收集表的分享链接，只从本机绑定读；没配就是空串（老标题）。"""
        config = self.title_display
        return '' if config is None else config.approve_entry_url.strip()

    def _nudge_approver(self, request: StageRequest, display=None):
        """Send the approver a todo for this stage; the entry row stays the truth.

        Recipient is the loan's approver. This todo carries no authoritative
        state — the conclusion is read from the entry row's decision column — so
        its failure is swallowed on purpose: a lost nudge must not report a
        created entry row as an unverified stage, and it is sent after the entry
        row exists so nobody is called to a stage that is not there.
        """
        try:
            payload = self.transport.exchange('todo.create', self._stage_arguments(
                request, f'{request.operation_id}:approver-todo',
                request.loan.approver.user_id, display))
            require_todo_envelope(payload)
        except DingTalkShapeError:  # 超时、业务拒绝、形态异常：只是催办没发出去
            return None
        return payload

    def query_stage(self, request: StageRequest) -> StageReceipt:
        try:
            payload = self.transport.exchange('stage.query', {
                'operation_id': request.operation_id,
            })
            result = require_envelope(payload).get('result')
            require(isinstance(result, dict), Code.EVIDENCE)
            receipt = self._stage_from_query(request, result)
        except UnknownResultError:
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
        except (BusinessErrorResponse, DingTalkShapeError, ContractError, KeyError, TypeError, ValueError):
            receipt = StageReceipt(request.operation_id, Outcome.UNKNOWN)
        self.journal.save_receipt(receipt)
        return receipt

    def _record_cells(self, ref):
        try:
            payload = self.transport.exchange('record.query', {
                'tenant_id': ref.tenant_id,
                'container_id': ref.container_id,
                'resource_id': ref.resource_id,
            })
            records = extract_records(payload)
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except DingTalkShapeError as exc:
            _closed(exc)
        require(len(records) == 1, Code.EVIDENCE)
        record = records[0]
        try:
            require(record_id(record) == ref.resource_id, Code.WRONG_LOAN)
            return record_cells(record)
        except DingTalkShapeError as exc:
            _closed(exc)

    def _put_record(self, ref, cells):
        payload = self.transport.exchange('record.update', {
            'tenant_id': ref.tenant_id,
            'container_id': ref.container_id,
            'resource_id': ref.resource_id,
            'cells': cells,
        })
        require_envelope(payload)

    def _stage_from_query(self, request, result):
        kind = result.get('kind')
        resource_id = result.get('resource_id')
        require(isinstance(resource_id, str) and resource_id, Code.EVIDENCE)
        container = result.get('container')
        require(isinstance(container, str) and container, Code.EVIDENCE)
        created = str(result.get('creation_evidence') or '')
        readback = str(result.get('readback_evidence') or '')
        if kind == 'form':
            source = Resource('form', request.loan.ref.tenant_id, container, resource_id)
            receipt = StageReceipt(request.operation_id, Outcome.VERIFIED, source,
                                   creation_evidence=created, readback_evidence=readback)
            return replace(receipt, outcome=verify_stage(request, receipt))
        if kind == 'todo':
            source = Resource('todo', request.loan.ref.tenant_id, container, resource_id)
            binding = self._binding_from_stage(request.loan, source, result)
            require(binding.contact == request.actor, Code.WRONG_PERSON)
            receipt = StageReceipt(request.operation_id, Outcome.VERIFIED, source, binding,
                                   created, readback)
            return replace(receipt, outcome=verify_stage(request, receipt))
        raise ContractError(Code.EVIDENCE)

    def _binding_from_stage(self, loan, source, queried):
        require(queried.get('kind') == 'todo', Code.EVIDENCE)
        require(queried.get('resource_id') == source.resource_id, Code.EVIDENCE)
        require(queried.get('loan_id') == loan.ref.resource_id, Code.WRONG_LOAN)
        require(queried.get('loan_container') == loan.ref.container_id, Code.WRONG_LOAN)
        require(queried.get('config_version') == loan.config_version, Code.CONFIG)
        created = str(queried.get('creation_evidence') or '')
        readback = str(queried.get('readback_evidence') or '')
        text(created)
        text(readback)
        raw = queried.get('internal_id')
        if isinstance(raw, bool) or raw is None:
            raise ContractError(Code.EVIDENCE)
        if isinstance(raw, int):
            internal_id = format(raw, 'd')
        elif isinstance(raw, str):
            internal_id = raw
        else:
            raise ContractError(Code.EVIDENCE)
        contact = Identity('contact', loan.ref.tenant_id, str(queried['contact']))
        internal = Identity('todo', contact.tenant_id, internal_id)
        return IdentityBinding(contact, internal, source, created, readback)

    def resolve_return_form_loan(self, source, hint_ref):
        """Return the sole borrowed loan a return-form row names, else None.

        ``#77``: when the row also carries the optional「归还物品」answer, the
        match narrows from "this borrower's only open loan" to "this borrower's
        only open loan of that item". Only the borrower knows which tool comes
        back, so the answer is read, never guessed.
        """
        if source.kind != 'form' or self.return_form_fields is None:
            return None
        cells = self._record_cells(source)
        if not self._is_return_form_submission(cells, source):
            return None
        actor = _identity(cells, self.return_form_fields.borrower, source.tenant_id)
        loan_container = self.loan_container or hint_ref.container_id
        text(loan_container)
        matches = self._matching_borrowed_loans(
            source.tenant_id, loan_container, actor.user_id, cells)
        require(len(matches) == 1, Code.EVIDENCE)
        matched = Resource('record', source.tenant_id, loan_container, matches[0])
        return matched

    def _matching_borrowed_loans(self, tenant_id, loan_container, borrower_user_id, cells):
        """Open loans of one borrower, narrowed by the「归还物品」answer when given.

        No usable answer: exactly the pre-#77 borrower-only list. With one, only
        loans whose item is that item survive, and the caller still demands a
        single match — two loans of the *same* item stay blocked, they are not a
        tie we may break.

        Candidates are re-read one by one rather than filtered platform-side so
        that a row we cannot read fails closed (``EVIDENCE``). Silently dropping
        an unreadable row would turn "more than one open loan" into a confident
        single match.
        """
        loan_ids = self._borrowed_loan_ids(tenant_id, loan_container, borrower_user_id)
        wanted = self._return_item_value(cells)
        if not wanted:
            return loan_ids
        matched = []
        for loan_id in loan_ids:
            ref = Resource('record', tenant_id, loan_container, loan_id)
            if self.read_loan(ref).item.resource_id == wanted:
                matched.append(loan_id)
        return matched

    def _return_item_value(self, cells):
        """Optional「归还物品」answer as text; ``''`` when unbound or unfilled.

        Absent or null means the human left the question empty (that is how the
        platform reports an unfilled cell), so the pre-#77 path applies. Anything
        present but unreadable is a shape we have not observed: fail closed
        instead of matching on a guess.
        """
        field_id = self.return_form_fields.item
        if not field_id or field_id not in cells or cells[field_id] is None:
            return ''
        value = cells[field_id]
        if isinstance(value, str):
            return value.strip()
        try:
            return read_single_select(cells, field_id).name.strip()
        except DingTalkShapeError as exc:
            _closed(exc)

    def _cell_has_text(self, cells, field_id):
        if field_id not in cells or cells[field_id] is None:
            return False
        try:
            return bool(read_text(cells, field_id).strip())
        except DingTalkShapeError:
            return False

    def _is_return_form_submission(self, cells, source):
        if self.return_form_fields is None:
            return False
        if self.entry_container and source.container_id != self.entry_container:
            return False
        entry = self.entry_fields
        if self._cell_has_text(cells, entry.loan_id):
            return False
        if self._cell_has_text(cells, entry.loan_container):
            return False
        fields = self.return_form_fields
        return (fields.borrower in cells and cells[fields.borrower] is not None
                and fields.occurred_at in cells and cells[fields.occurred_at] is not None)

    def _borrowed_loan_ids(self, tenant_id, loan_container, borrower_user_id):
        payload = self.transport.exchange('loan.query_borrowed', {
            'tenant_id': tenant_id,
            'loan_container': loan_container,
            'borrower': borrower_user_id,
        })
        result = require_envelope(payload).get('result')
        require(isinstance(result, dict), Code.EVIDENCE)
        loan_ids = result.get('loan_ids')
        require(isinstance(loan_ids, list), Code.EVIDENCE)
        cleaned = []
        for item in loan_ids:
            require(isinstance(item, str) and item.strip(), Code.EVIDENCE)
            cleaned.append(item)
        return cleaned

    def pending_applications(self, tenant_id):
        """Row refs of the application collection result table (no cells).

        The result table is the application's source of truth; this scan answers
        "which rows exist", never "which rows are new" — the caller owns the
        idempotency register. Only opaque record ids travel back, so a scan
        result can be reported without names or business numbers.
        """
        require(bool(self.application_container.strip()), Code.CONFIG)
        text(tenant_id)
        try:
            payload = self.transport.exchange('application.list', {
                'tenant_id': tenant_id,
                'container_id': self.application_container,
            })
            rows = query_rows(payload)
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except DingTalkShapeError as exc:
            _closed(exc)
        return tuple(Resource('form', tenant_id, self.application_container, row['recordId'])
                     for row in rows)

    def read_application(self, source):
        """One application row as a validated draft; every gap fails closed.

        缺项、缺失字段映射、形态未观察过都不补默认值：调用方据此按跳过记账。
        """
        fields = self.apply_fields
        require(fields is not None, Code.CONFIG)
        require(source.kind == 'form' and source.container_id == self.application_container,
                Code.EVIDENCE)
        # 声明检查先做：没声明这一格是 CONFIG，不是「去读一个空的字段 ID」。
        self._declared(fields.item_container)
        self._declared(fields.item_id)
        self._declared(fields.due_at)
        cells = self._record_cells(source)
        try:
            borrower = _identity(cells, fields.borrower, source.tenant_id)
            occurred = read_datetime(cells, fields.occurred_at)
            quantity = _int_count(cells, fields.quantity, zero=False)
            if fields.physical_ids in cells and cells[fields.physical_ids] is not None:
                physical_ids = _text_list(cells, fields.physical_ids)
            else:
                physical_ids = ()
            item_container = read_text(cells, fields.item_container)
            item_id = read_text(cells, fields.item_id)
            due_at = read_datetime(cells, fields.due_at)
        except ContractError:
            raise
        except (DingTalkShapeError, KeyError) as exc:
            _closed(exc)
        return ApplicationDraft(
            source=source,
            item=Resource('record', source.tenant_id, item_container, item_id),
            borrower=borrower,
            quantity=quantity,
            physical_ids=physical_ids,
            occurred_at=occurred,
            due_at=due_at,
        )

    def create_application_loan(self, draft: ApplicationDraft, binding, lease):
        """Create the ledger row for one application and prove it by readback.

        The row starts at ``awaiting_approval`` with the application marker in
        「申请证据」, so a later pass can adopt a half-finished attempt by exact
        search instead of creating a second row. Approver, manager and config
        version come from the binding only — never from the application row.
        """
        self.leases.assert_held(lease)
        container = self._declared(self.loan_container)
        intended = draft.loan(draft.pending_ref(container), binding)
        check_binding(binding, intended)
        try:
            payload = self.transport.exchange('loan.create', {
                'tenant_id': intended.ref.tenant_id,
                'container_id': container,
                'cells': encode_loan(intended, self.fields),
            })
            created_id = created_record_id(payload)
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except DingTalkShapeError as exc:
            # 受理形态不明：可能已建出，交给下一次对账认领，绝不重发。
            _closed(exc, Code.UNKNOWN)
        ref = Resource('record', intended.ref.tenant_id, container, created_id)
        return self._readback_loan(ref, intended)

    def find_application_loan(self, source, lease):
        """The ledger row already carrying this application's marker, else None.

        Exact marker match only: zero matches mean "no row carries it", several
        matches are ambiguous and fail closed. A match is still read back in full
        before it is handed out.
        """
        self.leases.assert_held(lease)
        container = self._declared(self.loan_container)
        marker = application_marker(source)
        try:
            payload = self.transport.exchange('loan.find_application', {
                'tenant_id': source.tenant_id,
                'container_id': container,
                'marker': marker,
            })
            found = [row['recordId'] for row in query_rows(payload)]
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except DingTalkShapeError as exc:
            _closed(exc)
        require(len(found) <= 1, Code.EVIDENCE)
        if not found:
            return None
        ref = Resource('record', source.tenant_id, container, found[0])
        loan = self.read_loan(ref)
        require(loan.application_evidence == marker, Code.EVIDENCE)
        return ref

    def _readback_loan(self, ref, intended):
        """Read the just-created row back; only an exact match is a success.

        Unreadable or mismatching readback keeps the attempt UNKNOWN: the row may
        exist, so the caller must adopt it later rather than write a second one.
        """
        try:
            created = self.read_loan(ref)
        except ContractError as exc:
            if exc.code == Code.IDENTITY:
                raise
            raise ContractError(Code.UNKNOWN) from exc
        require(created == replace(intended, ref=ref), Code.READBACK)
        return ref

    def _declared(self, field_id):
        """Field id from a map, or fail closed when this instance never set it.

        ``unset:<key>`` is how a binding says "this instance has no such
        question" (same convention the application read side already uses for
        逐件编号); it is not a field id and must never be queried as one.
        """
        if not _is_declared(field_id):
            raise ContractError(Code.CONFIG)
        return field_id

    def _read_form_event(self, loan, source):
        cells = self._record_cells(source)
        if (self.apply_fields is not None
                and source.container_id == self.application_container):
            return self._read_application_event(loan, source, cells)
        if self._is_return_form_submission(cells, source):
            return self._read_return_form_event(loan, source, cells)
        return self._read_entry_form_event(loan, source, cells)

    def _read_return_form_event(self, loan, source, cells):
        fields = self.return_form_fields
        try:
            actor = _identity(cells, fields.borrower, loan.ref.tenant_id)
            occurred = read_datetime(cells, fields.occurred_at)
            loan_container = self.loan_container or loan.ref.container_id
            matches = self._matching_borrowed_loans(
                source.tenant_id, loan_container, actor.user_id, cells)
            require(len(matches) == 1, Code.EVIDENCE)
            require(matches[0] == loan.ref.resource_id, Code.WRONG_LOAN)
            require(actor == loan.borrower, Code.WRONG_PERSON)
            require(loan.state == State.BORROWED, Code.STATE)
            return_ref = Resource(
                'record', source.tenant_id, source.container_id, source.resource_id)
            require(return_ref != loan.ref, Code.WRONG_LOAN)
            action = Action.REQUEST_RETURN
        except ContractError:
            raise
        except DingTalkShapeError as exc:
            _closed(exc)
        return Event(
            action, f'{source.resource_id}:{action.value}', loan.ref, source,
            actor, occurred, loan.config_version, 'form', True,
            return_ref=return_ref, quantity=loan.quantity, physical_ids=loan.physical_ids,
            evidence_ref=f'form:{source.resource_id}',
        )

    def _read_application_event(self, loan, source, cells):
        fields = self.apply_fields
        due_field = fields.due_at
        try:
            action = Action.APPLY
            actor = _identity(cells, fields.borrower, loan.ref.tenant_id)
            occurred = read_datetime(cells, fields.occurred_at)
            quantity = _int_count(cells, fields.quantity, zero=False)
            if fields.physical_ids in cells and cells[fields.physical_ids] is not None:
                physical_ids = _text_list(cells, fields.physical_ids)
            else:
                physical_ids = ()
            # 申请行上的归还时间：#78 之后受理要比对它与台账行一致。绑定没声明这一格时
            # 不读、也不比（None），这是启用检查项，不是「读到了空值」。
            due_at = read_datetime(cells, due_field) if _is_declared(due_field) else None
        except ContractError:
            raise
        except DingTalkShapeError as exc:
            _closed(exc)
        return Event(action, f'{source.resource_id}:{action.value}', loan.ref, source,
                     actor, occurred, loan.config_version, 'form', True,
                     quantity=quantity, physical_ids=physical_ids,
                     evidence_ref=f'form:{source.resource_id}', due_at=due_at)

    def _entry_form_action(self, cells, fields):
        if fields.decision in cells and cells[fields.decision] is not None:
            return _form_action(read_single_select(cells, fields.decision).name)
        if fields.action in cells and cells[fields.action] is not None:
            return _form_action(read_text(cells, fields.action))
        raise MissingFieldError('阶段入口缺少 decision 或 action')

    def _read_entry_form_event(self, loan, source, cells):
        fields = self.entry_fields
        try:
            require(read_text(cells, fields.loan_container) == loan.ref.container_id,
                    Code.WRONG_LOAN)
            require(read_text(cells, fields.loan_id) == loan.ref.resource_id,
                    Code.WRONG_LOAN)
            require(read_text(cells, fields.config_version) == loan.config_version,
                    Code.CONFIG)
            action = self._entry_form_action(cells, fields)
            if action in (Action.APPLY, Action.REQUEST_RETURN):
                actor = _identity(cells, fields.borrower, loan.ref.tenant_id)
            elif action == Action.CANCEL:
                actor = _identity(cells, fields.manager, loan.ref.tenant_id)
            else:
                actor = _identity(cells, fields.approver, loan.ref.tenant_id)
            if not self._cell_has_text(cells, fields.occurred_at):
                raise MissingFieldError(
                    '阶段入口缺少「发生时间」：决定与发生时间都要真人填，引擎不预填')
            occurred = read_datetime(cells, fields.occurred_at)
            return_ref = None
            quantity = None
            physical_ids = ()
            if action in (Action.APPLY, Action.REQUEST_RETURN):
                quantity = _int_count(cells, fields.quantity, zero=False)
                physical_ids = _text_list(cells, fields.physical_ids)
            if action == Action.REQUEST_RETURN:
                return_ref = Resource(
                    'record', loan.ref.tenant_id,
                    read_text(cells, fields.return_container),
                    read_text(cells, fields.return_id),
                )
        except ContractError:
            raise
        except (DingTalkShapeError, KeyError) as exc:
            _closed(exc)
        return Event(action, f'{source.resource_id}:{action.value}', loan.ref, source,
                     actor, occurred, loan.config_version, 'form', True,
                     return_ref=return_ref, quantity=quantity, physical_ids=physical_ids,
                     evidence_ref=f'form:{source.resource_id}')

    def _read_todo_event(self, loan, source):
        try:
            payload = self.transport.exchange('todo.get', {
                'tenant_id': source.tenant_id,
                'task_id': source.resource_id,
            })
            envelope = require_todo_envelope(payload)
            detail = read_todo_detail(envelope)
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except DingTalkShapeError as exc:
            _closed(exc, Code.UNKNOWN)
        events = completion_events(detail)
        # 待办还没被点完成：这是「等人工」，不是「证据不足」。驱动按本码区分，
        # 不要把未完成和「已完成但证据读不出来」混成一句提示。
        require(bool(events), Code.STATE)
        actors = {item.actor.value for item in events}
        require(len(actors) == 1, Code.EVIDENCE)
        actor_ref = events[0].actor
        actor_ref.require(TODO)
        try:
            require(finish_time(detail) is not None, Code.EVIDENCE)
            occurred = completion_at(detail)
        except DingTalkShapeError as exc:
            _closed(exc)
        require(occurred is not None, Code.EVIDENCE)
        try:
            meta_payload = self.transport.exchange('stage.query', {
                'task_id': source.resource_id,
            })
            queried = require_envelope(meta_payload).get('result')
            require(isinstance(queried, dict), Code.EVIDENCE)
            binding = self._binding_from_stage(loan, source, queried)
        except UnknownResultError as exc:
            _closed(exc, Code.UNKNOWN)
        except BusinessErrorResponse as exc:
            _closed(exc, _business_code(exc))
        except ContractError:
            raise
        except (DingTalkShapeError, KeyError, TypeError, ValueError) as exc:
            _closed(exc)
        executors = executor_refs(detail)
        require(executors, Code.EVIDENCE)
        require(actor_ref.same_person_as(executors[0]), Code.WRONG_PERSON)
        completer = Identity('todo', source.tenant_id, actor_ref.value)
        binding = replace(binding, internal=completer)
        action = Action(queried['action'])
        require(action in (Action.ISSUE, Action.RETURN), Code.STATE)
        return Event(
            action, f'{source.resource_id}:completion', loan.ref, source,
            completer, occurred, loan.config_version,
            'todo_completion', True, binding=binding,
            evidence_ref=f'todo:{source.resource_id}:completion',
        )
