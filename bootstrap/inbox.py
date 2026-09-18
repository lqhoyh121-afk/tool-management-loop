"""轮询前自动发现新申请并登记（issue #78）。

工作队列过去只由人工填写：每笔真人申请都要操作员「建台账行 + 往 ``sources.json``
追加 ``kind=apply`` 引用」才动。本模块在每轮驱动消费队列之前扫一次申请收集表结果表，
把这件事自己做掉，并守三条硬线：

* **幂等** —— 一行申请只出一条台账行、一条队列引用。判定依据是本机已有引用
  （工作队列 + 操作日志 + 本机登记册）与台账行自己的「申请证据」格
  （``form:<申请行 id>``）：重启、重跑、同一行被扫两次都只多一条。**建行之前先按
  链路键查台账侧**：登记册在运行目录里，丢了不代表台账行没建过，所以查不到才允许
  新建（`_create`）。
* **有水位** —— 只处理申请时间（`apply_fields.occurred_at`）≥ 启用水位的行。水位是
  绑定里的 ``application_intake.since``；**没配水位就一行都不建**（只出报告），因为
  「表里哪些行是启用前就存在的历史行」本机无从判断，而一次误批量建行会推出早已
  处理完的审批待办。水位之前的行单独记账（``history``），既不是跳过原因也不是失败。
* **fail closed** —— 读不全、必填缺失、字段映射没配、同一行重复出现、上次写入结果
  不明，一律**不猜**：按跳过记账并给出原因码；结果不明时下一轮只按链路键**认领**
  已建出的行，绝不重发一次新建。
* **脱敏** —— 报告只出现申请行 id、台账行 id、结果表容器与原因码；人名、数量等真实
  值只留在本机运行目录（登记册与队列文件里）。

闸门不变：登记前先过 ``assert_business_allowed``（绑定 + 机器租约），新建/认领的台账行
必须再回读一次并满足 ``check_binding``；审批人与管理人只来自绑定，不来自申请行。

本模块不发平台请求：申请表扫描与台账行读写都由注入的申请端口（DingTalkAdapter）
完成，测试注入替身。
"""
import json
from dataclasses import dataclass
from pathlib import Path

from contracts.model import Code, ContractError, Resource, State, aware, require
from contracts.ports import FormRow, check_binding, row_ref
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
    """一轮发现的账目：扫描 / 本机已有引用 / 本轮登记 / 跳过 / 历史行。

    ``scan_code`` 非空表示扫描本身没跑完（读表失败、发现阶段抛异常等），此时没有
    任何登记发生 —— 缺表不等于空表。``container`` 是本轮查的结果表，``scanned``
    就是这次回读到的行数：0 行且没有 ``scan_code`` 才是「表真的为空」。``history``
    是水位之前的历史行 id（不建行、不登记）。``since`` 是本轮用的水位（未配时为空）。
    """

    scanned: int = 0
    known: int = 0
    findings: tuple = ()
    skipped: tuple = ()
    scan_code: str = ''
    scan_note: str = ''
    container: str = ''
    history: tuple = ()
    since: str = ''


def format_intake_lines(report):
    """报告文本：只有计数、平台记录 id、结果表容器与原因码，不含人名/数量/业务值。"""
    lines = ['申请发现：扫描 {scanned}，本机已有 {known}，本轮登记 {new}，跳过 {skipped}'.format(
        scanned=report.scanned, known=report.known,
        new=len(report.findings), skipped=len(report.skipped))]
    if report.container:
        # 查的哪张表、回读到几行、是不是真为空：扫描 0 行必须与「读不到」区分开。
        verdict = ''
        if not report.scan_code:
            verdict = ('（读表成功，本次回读 0 行 = 结果表当前确实为空，不是读不到）'
                       if report.scanned == 0 else '（读表成功）')
        lines.append('  来源：申请表 container={container}，本次回读 {rows} 行{verdict}'.format(
            container=report.container, rows=report.scanned, verdict=verdict))
    if report.scan_code:
        lines.append('  跳过 申请 row=- {code}（{note}，本轮未登记任何申请）'.format(
            code=report.scan_code, note=report.scan_note or '本轮未读到申请表'))
    if report.since:
        lines.append(f'  水位：只处理申请时间 ≥ {report.since} 的申请行')
    elif not report.scan_code:
        lines.append('  水位未配置：本轮只出报告、不建任何台账行（dry-run）；'
                     '在绑定里配 application_intake.since 之后才会自动登记新申请')
    if report.history:
        lines.append('  历史行（申请时间在水位之前，不建行也不登记）：{ids}'.format(
            ids='，'.join(report.history)))
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
    """一轮申请发现。所有写入都在绑定与机器租约之下。

    ``since`` 是启用水位（``application_intake.since``，带时区）：只处理申请时间在
    水位之后的申请行；**没配水位就一行都不建**，只出报告。第一次启用时表里已有的行
    在文件里没有任何记载（本机登记册、队列、日志全都不认识它们），所以「首轮要不要
    把它们当新申请」本机没有判据 —— 一律不建是刻意的保守默认。
    """

    def __init__(self, engine, port, sources, store, locks, register_path, since=None):
        self.engine = engine
        self.port = port
        self.sources = sources
        self.store = store
        self.locks = locks
        self.register = InboxRegister(register_path)
        if since is not None:
            aware(since)
        self.since = since

    def run(self):
        assert_business_allowed(self.engine.binding, self.engine.lease, self.locks)
        entries = self.register.entries()
        known_ids = self._known_row_ids(entries)
        container = getattr(self.port, 'application_container', '')
        try:
            rows = self.port.pending_applications(self.engine.binding.account.tenant_id)
        except ContractError as exc:
            if exc.code == Code.INSTANCE:
                raise
            return self._report(container, skipped=(IntakeSkip('-', exc.code.value),),
                                scan_code=exc.code.value)
        scanned = 0
        known = 0
        findings = []
        skipped = []
        history = []
        seen = set()
        for row in rows:
            scanned += 1
            # 扫描行（``FormRow``）带着扫描时的单元格：下面只把它折算成引用用 —— 要建行
            # 的行一律拿裸引用精读一次，扫描内容只用来「确证不用管」（见 _scanned_history）。
            ref = row_ref(row)
            row_id = ref.resource_id
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
                    loan_ref = self._adopt(ref)
                except ContractError as exc:
                    if exc.code == Code.INSTANCE:
                        raise
                    skipped.append(IntakeSkip(row_id, exc.code.value))
                    continue
                if loan_ref is None:
                    # 记过「正在建」但按链路键找不到行：不重发，等人工核。
                    skipped.append(IntakeSkip(row_id, Code.UNKNOWN.value))
                    continue
                findings.append(RegisteredApply(ref, loan_ref))
                continue
            if row_id in known_ids:
                known += 1
                continue
            if self.since is None:
                # 没有水位就不建：首次启用不得把表里的历史行全建成台账行。
                skipped.append(IntakeSkip(row_id, Code.CONFIG.value))
                continue
            # 扫描内容在手上时先看时间格（本地解码，不发平台调用）：确证在水位之前的行不
            # 值得再发一次精读。判不准（没带单元格、时间格解不出来）照旧往下逐行精读。
            if self._scanned_history(row):
                history.append(row_id)
                continue
            try:
                draft = self.port.read_application(ref)
            except ContractError as exc:
                if exc.code == Code.INSTANCE:
                    raise
                skipped.append(IntakeSkip(row_id, exc.code.value))
                continue
            if draft.occurred_at < self.since:
                # 水位之前的行是申请启用前就存在的历史行：不处理、不建行，单独记账。
                history.append(row_id)
                continue
            try:
                loan_ref = self._create(row_id, ref, draft)
            except ContractError as exc:
                if exc.code == Code.INSTANCE:
                    raise
                skipped.append(IntakeSkip(row_id, exc.code.value))
                continue
            findings.append(RegisteredApply(ref, loan_ref))
        return self._report(container, scanned=scanned, known=known,
                            findings=tuple(findings), skipped=tuple(skipped),
                            history=tuple(history))

    def _scanned_history(self, row):
        """扫描内容能不能确证「这一行在水位之前」（只解时间格，不构造草稿）。

        只在端口把**扫描时的单元格**一起给了（``FormRow``）时才问，而且问端口里那个只解
        时间格的探针：构造申请草稿会顺带读一整张库存表（按名称解析物品），预筛一次等于
        再加一次全表读。判不准（探针不存在、时间格解不出来）返回 ``False`` —— 预筛只会
        **少做**平台调用，永远不会把「判不准」当成「不用管」。
        """
        if not isinstance(row, FormRow) or self.since is None:
            return False
        probe = getattr(self.port, 'scanned_application_time', None)
        if probe is None:
            return False
        try:
            occurred = probe(row)
        except ContractError:
            return False
        return occurred is not None and occurred < self.since

    def _report(self, container, **fields):
        return IntakeReport(container=container, since=self._since_text(), **fields)

    def _since_text(self):
        return '' if self.since is None else self.since.isoformat()

    def _create(self, row_id, row, draft):
        """新行入册：先按链路键查台账侧，命中的直接认领，未命中才发新建。

        登记册是本机文件，删掉/换机之后它不认识任何行，而台账行里的「申请证据」格
        （``form:<申请行 id>``）仍然认识 —— 那才是幂等判据。所以建行之前必须先精确
        查一次：查不到任何行才允许新建，查得到就只登记（不重发）。
        """
        existing = self.port.find_application_loan(row, self.engine.lease)
        if existing is not None:
            self._commit(row_id, row, existing)
            return existing
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
            for ref in referenced_sources(intent, receipt):
                if ref.kind == 'form' and ref.container_id == container:
                    known.add(ref.resource_id)
        return known


def referenced_sources(intent, receipt):
    """Journal 条目里出现的来源引用：读侧 intent 的事件来源与阶段回执来源。

    申请发现（#78）与归还发现（#87）共用这一份口径：本机「已经认识」哪些来源行，
    只由工作队列与操作日志回答，不靠第二本登记册。
    """
    refs = []
    event = getattr(intent, 'event', None)
    source = getattr(event, 'source', None)
    if isinstance(source, Resource):
        refs.append(source)
    receipt_source = getattr(receipt, 'source', None)
    if isinstance(receipt_source, Resource):
        refs.append(receipt_source)
    return tuple(refs)
