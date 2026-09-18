"""轮询前自动发现新申请并登记（issue #78）。

工作队列过去只由人工填写：每笔真人申请都要操作员「建台账行 + 往 ``sources.json``
追加 ``kind=apply`` 引用」才动。本模块在每轮驱动消费队列之前扫一次申请收集表结果表，
把这件事自己做掉，并守三条硬线：

* **幂等** —— 一行申请只出一条台账行、一条队列引用。判定依据是本机已有引用
  （工作队列 + 操作日志 + 本机登记册）与台账行自己的「申请证据」格
  （``form:<申请行 id>``）：重启、重跑、同一行被扫两次都只多一条。
* **fail closed** —— 读不全、必填缺失、字段映射没配、同一行重复出现、上次写入结果
  不明，一律**不猜**：按跳过记账并给出原因码；结果不明时下一轮只按链路键**认领**
  已建出的行，绝不重发一次新建。
* **脱敏** —— 报告只出现申请行 id、台账行 id 与原因码；人名、数量等真实值只留在本机
  运行目录（登记册与队列文件里）。

闸门不变：登记前先过 ``assert_business_allowed``（绑定 + 机器租约），新建/认领的台账行
必须再回读一次并满足 ``check_binding``；审批人与管理人只来自绑定，不来自申请行。

本模块不发平台请求：申请表扫描与台账行读写都由注入的申请端口（DingTalkAdapter）
完成，测试注入替身。
"""
import json
from dataclasses import dataclass
from pathlib import Path

from contracts.model import Code, ContractError, Resource, State, require
from contracts.ports import check_binding
from integrations.dingtalk.application import application_marker

from .gate import assert_business_allowed
from .snapshot import encode_resource

REGISTER_NAME = 'inbox-applications.json'

_STATUS_CREATING = 'creating'
_STATUS_REGISTERED = 'registered'


@dataclass(frozen=True)
class RegisteredApply:
    """一行申请已登记的成果：申请行引用 + 对应台账行引用。

    队列仍以 ``kind='apply'`` 的「台账行 + 来源」对储存，所以这个类型就是队列
    写入的最小单位；``kind`` 属性让队列侧不必再判断来源种类。
    """

    source: Resource
    loan_ref: Resource

    @property
    def kind(self):
        return 'apply'


@dataclass(frozen=True)
class IntakeSkip:
    """一行申请没有进流程，以及为什么。原因码是公共 Code，行 id 是平台记录 id。"""

    row_id: str
    code: str

    def line(self):
        return f'  跳过 申请 row={self.row_id} {self.code}'


@dataclass(frozen=True)
class IntakeReport:
    """一轮发现的账目：扫描 / 本机已有引用 / 本轮登记 / 跳过。

    ``scan_code`` 非空表示扫描本身没跑完（读表失败等），此时没有任何登记发生 ——
    缺表不等于空表。
    """

    scanned: int = 0
    known: int = 0
    findings: tuple = ()
    skipped: tuple = ()
    scan_code: str = ''


def format_intake_lines(report):
    """报告文本：只有计数、平台记录 id 与原因码，不含人名/数量/业务值。"""
    lines = ['申请发现：扫描 {scanned}，本机已有 {known}，本轮登记 {new}，跳过 {skipped}'.format(
        scanned=report.scanned, known=report.known,
        new=len(report.findings), skipped=len(report.skipped))]
    if report.scan_code:
        lines.append(f'  跳过 申请 row=- {report.scan_code}（本轮未读到申请表，未登记任何申请）')
    for finding in report.findings:
        lines.append(f'  登记 申请 row={finding.source.resource_id}')
    for skip in report.skipped:
        lines.append(skip.line())
    return lines


class InboxRegister:
    """本机登记册：申请行 id → 台账行。只落运行目录，不进仓库、不外发。"""

    def __init__(self, path):
        self.path = Path(path)

    def entries(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        require(isinstance(data, dict), Code.INVALID)
        rows = data.get('rows')
        require(isinstance(rows, dict), Code.INVALID)
        return rows

    def mark_creating(self, row_id):
        """写「正在建」先于发写：结果不明时下一次才认领得到，而不是当没发生过。"""
        self._save(row_id, _STATUS_CREATING, None)

    def mark_registered(self, row_id, loan_ref):
        self._save(row_id, _STATUS_REGISTERED, loan_ref)

    def _save(self, row_id, status, loan_ref):
        require(isinstance(row_id, str) and row_id.strip(), Code.INVALID)
        rows = self.entries()
        rows[row_id] = {'status': status,
                        'loan': None if loan_ref is None else encode_resource(loan_ref)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        payload = {'rows': rows}
        tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + '\n',
                       encoding='utf-8')
        tmp.replace(self.path)


class ApplicationIntake:
    """一轮申请发现。所有写入都在绑定与机器租约之下。"""

    def __init__(self, engine, port, sources, store, locks, register_path):
        self.engine = engine
        self.port = port
        self.sources = sources
        self.store = store
        self.locks = locks
        self.register = InboxRegister(register_path)

    def run(self):
        assert_business_allowed(self.engine.binding, self.engine.lease, self.locks)
        entries = self.register.entries()
        known_ids = self._known_row_ids(entries)
        try:
            rows = self.port.pending_applications(self.engine.binding.account.tenant_id)
        except ContractError as exc:
            if exc.code == Code.INSTANCE:
                raise
            return IntakeReport(skipped=(IntakeSkip('-', exc.code.value),),
                                scan_code=exc.code.value)
        scanned = 0
        known = 0
        findings = []
        skipped = []
        seen = set()
        for row in rows:
            scanned += 1
            row_id = row.resource_id
            if row_id in seen:
                # 同一行在一份清单里出现两次：不猜哪一次是真的，两次都不动。
                skipped.append(IntakeSkip(row_id, Code.DUPLICATE.value))
                continue
            seen.add(row_id)
            entry = entries.get(row_id)
            if entry is not None:
                status = entry.get('status') if isinstance(entry, dict) else None
                if status == _STATUS_REGISTERED:
                    known += 1
                    continue
                if status != _STATUS_CREATING:
                    skipped.append(IntakeSkip(row_id, Code.INVALID.value))
                    continue
                try:
                    loan_ref = self._adopt(row)
                except ContractError as exc:
                    if exc.code == Code.INSTANCE:
                        raise
                    skipped.append(IntakeSkip(row_id, exc.code.value))
                    continue
                if loan_ref is None:
                    # 记过「正在建」但按链路键找不到行：不重发，等人工核。
                    skipped.append(IntakeSkip(row_id, Code.UNKNOWN.value))
                    continue
                findings.append(RegisteredApply(row, loan_ref))
                continue
            if row_id in known_ids:
                known += 1
                continue
            try:
                loan_ref = self._create(row_id, row)
            except ContractError as exc:
                if exc.code == Code.INSTANCE:
                    raise
                skipped.append(IntakeSkip(row_id, exc.code.value))
                continue
            findings.append(RegisteredApply(row, loan_ref))
        return IntakeReport(scanned=scanned, known=known, findings=tuple(findings),
                            skipped=tuple(skipped))

    def _create(self, row_id, row):
        draft = self.port.read_application(row)
        # 先记账再发写：崩溃或结果不明时下一轮按链路键认领，不会第二条。
        self.register.mark_creating(row_id)
        loan_ref = self.port.create_application_loan(
            draft, self.engine.binding, self.engine.lease)
        self._commit(row_id, row, loan_ref)
        return loan_ref

    def _adopt(self, row):
        loan_ref = self.port.find_application_loan(row, self.engine.lease)
        if loan_ref is None:
            return None
        self._commit(row.resource_id, row, loan_ref)
        return loan_ref

    def _commit(self, row_id, row, loan_ref):
        """登记：先追加工作队列（去重），再记本机登记册。

        顺序不能反：登记册是「已登记」的判据，队列是下游对账的输入；先记登记册会
        留下「书上有、队列里没有」的缝，正是本 issue 要根治的那种静默停摆。
        """
        loan = self.engine.reader.read_loan(loan_ref)
        check_binding(self.engine.binding, loan)
        require(loan.application_evidence == application_marker(row), Code.EVIDENCE)
        require(loan.state not in (State.CLOSED, State.REJECTED, State.CANCELLED),
                Code.STATE)
        append = getattr(self.sources, 'append', None)
        if append is not None:
            append((RegisteredApply(row, loan_ref),))
        self.register.mark_registered(row_id, loan_ref)

    def _known_row_ids(self, entries):
        """本机任何来源引用里已经出现过的申请行 id（幂等判据）。

        只认申请容器里的 form 引用：其他表的记录 id 恰好相同不能替一行申请免登记。
        """
        container = getattr(self.port, 'application_container', '')
        known = set(entries)
        for item in self.sources.pending():
            ref = item.source
            if ref.kind == 'form' and ref.container_id == container:
                known.add(ref.resource_id)
        for operation_id in self.store.ids():
            try:
                intent, receipt = self.store.load(operation_id)
            except KeyError:
                continue
            for ref in _referenced_sources(intent, receipt):
                if ref.kind == 'form' and ref.container_id == container:
                    known.add(ref.resource_id)
        return known


def _referenced_sources(intent, receipt):
    """Journal 条目里出现的来源引用：读侧 intent 的事件来源与阶段回执来源。"""
    refs = []
    event = getattr(intent, 'event', None)
    source = getattr(event, 'source', None)
    if isinstance(source, Resource):
        refs.append(source)
    receipt_source = getattr(receipt, 'source', None)
    if isinstance(receipt_source, Resource):
        refs.append(receipt_source)
    return tuple(refs)
