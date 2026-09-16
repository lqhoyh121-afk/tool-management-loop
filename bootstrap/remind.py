"""Reminder pass wiring for the live deployment (T07).

``integrations/`` and ``scheduler/`` stay untouched: this module injects a real
clock, a ``chat.send`` sender and a file-backed dedup store into
``scheduler.runner.ReminderRunner``, which already owns the window, dedup and
unknown-send rules.

Loan candidates come from explicit ``--loan`` targets, the operation journal and
``runtime/sources.json``; every candidate is read fresh through the adapter
before any decision, so a loan that moved on stops reminding by itself.

This pass is not a ledger writer: it sends chat messages only, so it takes no
machine lease. Run it after the drive pass, with the network path ``dws`` needs
for ``chat send`` reachable.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contracts.model import Code, Resource, business_date, require
from contracts.ports import StageRequest
from scheduler.reminder import BEFORE_DUE, OVERDUE, evaluate, loan_prefix
from scheduler.runner import FileDedupStore, ReminderRunner

from .binding import field_maps_from_document, read_binding_document
from .drive import live_adapter
from .journal import FileJournal
from .paths import runtime_dir

LOCAL_TZ = timezone(timedelta(hours=8))
DUE_TODAY = 'due_today'
TITLES = {'before_due': '工器具归还提醒', 'due_today': '工器具归还到期提醒',
          'overdue': '工器具归还逾期提醒'}
WHEN = {'before_due': '请在到期前归还', 'due_today': '今天到期，请归还',
        'overdue': '已逾期，请尽快归还'}


class SystemClock:
    """Aware system clock; the runner converts to the Asia/Shanghai window."""

    def __init__(self, now=None):
        self.fixed = now

    def now(self):
        return self.fixed or datetime.now(timezone.utc)


class Receipts:
    """Per-key send receipts: durable evidence for the requery, never a ledger."""

    def __init__(self, path):
        self.path = Path(path)
        self.entries = {}
        if self.path.exists():
            self.entries = json.loads(self.path.read_text(encoding='utf-8'))

    def record(self, key, payload):
        ok = isinstance(payload, dict) and payload.get('success') is True
        entry = {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'ok': ok}
        if not ok:
            error = payload.get('error') if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get('code'):
                entry['error'] = f"{error['code']}: {error.get('message', '')}".strip()
            else:
                entry['error'] = 'no-envelope'
        self.entries[key] = entry
        self._flush()
        return ok

    def query(self, key):
        entry = self.entries.get(key)
        if not entry:
            return 'unknown'
        return 'ok' if entry.get('ok') else 'unknown'

    def _flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1, sort_keys=True),
                       encoding='utf-8')
        tmp.replace(self.path)


class ChatSender:
    """One chat message per reminder; no envelope means unknown, never retried."""

    def __init__(self, transport, receipts, describe):
        self.transport = transport
        self.receipts = receipts
        self.describe = describe

    def send(self, reminder):
        title, text = self.describe(reminder)
        payload = self.transport.exchange('chat.send', {
            'user': reminder.borrower.user_id,
            'title': title,
            'text': text,
        })
        return 'ok' if self.receipts.record(reminder.key, payload) else 'unknown'


def parse_ref(raw):
    """``tenant/container/resource`` into a record Resource; no guessed shapes."""
    parts = (raw or '').split('/')
    require(len(parts) == 3 and all(part.strip() for part in parts), Code.CONFIG)
    return Resource(kind='record', tenant_id=parts[0], container_id=parts[1],
                    resource_id=parts[2])


def _refs_from_intent(intent):
    if isinstance(intent, StageRequest):
        return [intent.loan.ref]
    refs = [intent.before.ref]
    if intent.after is not None:
        refs.append(intent.after.ref)
    return refs


def journal_refs(journal):
    """Every loan this machine has driven, in operation order, deduplicated."""
    seen, refs = set(), []
    for operation_id in journal.ids():
        loaded = journal.load(operation_id)
        if not loaded:
            continue
        intent = loaded[0]
        for ref in _refs_from_intent(intent):
            if ref.kind == 'record' and ref.resource_id not in seen:
                seen.add(ref.resource_id)
                refs.append(ref)
    return refs


def sources_refs(path):
    """Loan refs from the operator's source file: a top-level list of entries."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    items = data if isinstance(data, list) else data.get('loans')
    refs = []
    for item in items or []:
        raw = item.get('loan') if isinstance(item, dict) else None
        if isinstance(raw, dict) and raw.get('kind') == 'record':
            refs.append(Resource(kind='record', tenant_id=raw['tenant_id'],
                                 container_id=raw['container_id'],
                                 resource_id=raw['resource_id']))
    return refs


def dedupe_refs(refs):
    """One candidate per record: the journal and sources.json overlap by design."""
    seen, unique = set(), []
    for ref in refs:
        if ref.resource_id not in seen:
            seen.add(ref.resource_id)
            unique.append(ref)
    return unique


def dedupe_loans(loans):
    """One candidate per loan prefix, so a pass can never message twice."""
    seen, unique = set(), []
    for loan in loans:
        prefix = loan_prefix(loan)
        if prefix not in seen:
            seen.add(prefix)
            unique.append(loan)
    return unique


def read_loans(adapter, refs):
    """Read every candidate fresh; an unreadable one is reported, never assumed."""
    loans, skipped = [], []
    for ref in refs:
        try:
            loans.append(adapter.read_loan(ref))
        except Exception as error:                      # noqa: BLE001 - report, never guess
            skipped.append((ref, f'{type(error).__name__}: {error}'))
    return loans, skipped


def reminder_prefix(reminder):
    """Same rule as ``scheduler.reminder.loan_prefix``, applied to a reminder."""
    ref = reminder.loan_ref
    return f'{ref.tenant_id}::{ref.container_id}::{ref.resource_id}::'


def reminder_stage(reminder, loan):
    """Copy stage: the due day itself reads 'due today', later days read 'overdue'.

    The scheduler decides *when* to send; this only picks the wording, so a
    scheduler that starts the overdue run on the due day needs no second kind.
    """
    if reminder.kind == BEFORE_DUE:
        return BEFORE_DUE
    if loan is not None and reminder.send_day == business_date(loan.due_at).isoformat():
        return DUE_TODAY
    return OVERDUE


def make_describe(loans, tz=LOCAL_TZ):
    """Build the chat copy; the loan is matched by its frozen key prefix."""
    by_prefix = {loan_prefix(loan): loan for loan in loans}

    def describe(reminder):
        loan = by_prefix.get(reminder_prefix(reminder))
        due, count = reminder.send_day, ''
        if loan is not None:
            due = loan.due_at.astimezone(tz).strftime('%Y-%m-%d %H:%M')
            count = f' ×{loan.quantity}'
        stage = reminder_stage(reminder, loan)
        title = TITLES[stage]
        text = (f"你借用的工器具{count}，预计归还时间 {due}，{WHEN[stage]}。"
                f"归还时请找管理人完成归还确认。")
        return title, text

    return describe


def parse_now(raw):
    """``YYYY-MM-DD HH:MM`` in Asia/Shanghai: dry-run time travel, never a real send."""
    stamp = None
    try:
        stamp = datetime.strptime(raw, '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        stamp = None
    require(stamp is not None, Code.CONFIG)
    return stamp.replace(tzinfo=LOCAL_TZ)


def main(argv=None):
    parser = argparse.ArgumentParser(description='工器具归还提醒：按冻结口径发一轮，不写台账')
    parser.add_argument('--runtime', default=None, help='运行时目录，默认 runtime/')
    parser.add_argument('--loan', action='append', default=[],
                        help='tenant/container/resource，可重复；不填则用日志与 sources.json')
    parser.add_argument('--dry-run', action='store_true', help='只算不发、不落盘')
    parser.add_argument('--now', default=None,
                        help='仅与 --dry-run 同用：把「现在」定在 YYYY-MM-DD HH:MM（上海时区）预演那一轮')
    parser.add_argument('--skip-sources', action='store_true', help='不读 sources.json')
    args = parser.parse_args(argv)
    require(args.now is None or args.dry_run, Code.CONFIG)

    runtime = runtime_dir(args.runtime)
    document = read_binding_document(runtime)
    fields, entry_fields, apply_fields = field_maps_from_document(document)
    journal = FileJournal(runtime / 'operations')
    adapter = live_adapter(runtime, journal, None, document, fields, entry_fields,
                           apply_fields)

    refs = [parse_ref(raw) for raw in args.loan]
    if not refs:
        refs = journal_refs(journal)
        if not args.skip_sources:
            refs.extend(sources_refs(runtime / 'sources.json'))

    loans, skipped = read_loans(adapter, dedupe_refs(refs))
    loans = dedupe_loans(loans)
    receipts = Receipts(runtime / 'reminder-receipts.json')
    store = FileDedupStore(runtime / 'reminders.json')

    print(f'候选单 {len(loans)} 张，读不到 {len(skipped)} 张')
    for ref, reason in skipped:
        print(f'  跳过 {ref.resource_id}: {reason}')

    describe = make_describe(loans)
    if args.dry_run:
        clock = SystemClock(parse_now(args.now) if args.now else None)
        now = clock.now()
        if args.now:
            print(f'预演时刻：{now.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")}（上海）')
        for loan in loans:
            for reminder in evaluate(loan, now):
                if store.is_sent(reminder.key):
                    print(f'  [已发过] {reminder.kind} {reminder.send_day} '
                          f'{loan.ref.resource_id}')
                    continue
                title, text = describe(reminder)
                print(f'  [待发] {title} -> {reminder.borrower.user_id} '
                      f'({loan.ref.resource_id})')
                print(f'         {text}')
        print('dry-run：未发送、未落盘')
        return 0

    runner = ReminderRunner(SystemClock(), ChatSender(adapter.transport, receipts, describe),
                            receipts, store)
    runner.start()
    report = runner.run(loans)
    runner.stop()

    state = '窗口内' if report.window_open else '窗口外（不发）'
    print(f'提醒轮次 {report.started_at}：{state}，发出 {len(report.issued)} 条，'
          f'挂起阻塞 {report.blocked_loans} 张单')
    for reminder in report.issued:
        print(f'  已发 {reminder.kind} {reminder.send_day} -> {reminder.borrower.user_id} '
              f'({reminder.loan_ref.resource_id})')
    print(f'去重台账：{store.path}')
    print(f'发送回执：{receipts.path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
