"""轮询前自动发现新归还：归还表单行 → 工作队列引用（issue #87）。

工作队列过去只由人工填写：真人填完归还表单之后本机**不认识**那一行 —— 驱动只处理队列与
日志里出现过的单据，所以旧流程要操作员手工往 ``sources.json`` 追加一条 ``kind=event``
引用才动（#87 的最后一个手工桥）。本模块在每轮驱动消费队列之前扫一次归还表单所在的
结果表，把这件事自己做掉。做法与 #78 的申请发现同源：扫描 → 逐行校验 → 用**既有**的
``resolve_return_form_loan`` 定位唯一一张借出中的单 → 登记一条 ``kind=event`` 引用，
交给同一轮的驱动正常推进。

三条硬线（与 #78 同一口径）：

* **幂等** —— 同一行只登记一次。判据是本机**已有引用**：工作队列条目（``sources.json``）
  与操作日志里出现过的来源行。归还登记**不产生任何平台写入**（只追加一条队列引用），
  所以队列本身就是登记册 —— 没有第二本账，也就没有「登记册丢了」这条缝。队列与日志
  一起丢了的话，重放会被单据自身状态拦住（``resolve`` 只认借出中的单）。
* **fail closed** —— 表读不出来（``records: null`` 而 ``hasMore`` 不为 false、容器没
  声明、报文形态变了）、行读不出来、归还两格缺一、定位不到**唯一一张**借出中的单、
  定位到的单已经不在借出中 —— 一律按行记原因码并跳过，绝不猜，也绝不静默当
  「今天没有新归还」。
* **水位** —— ``return_intake.since``：只处理归还时间 ≥ 水位的行。**没配水位就一行都
  不登记**（只出报告），因为「表里哪些行是启用前就存在的历史行」本机无从判断，而一次
  误登记会立刻推出早已处理完的待归还确认待办。水位之前的行单独记账（``history``）。

脱敏：报告只出现行 id、结果表容器与原因码，外加固定的说明文案（下面的 ``*_NOTE``）；
人名、物品、单据值只落本机运行目录。

本模块不发平台请求：扫描、逐行读取与单据定位都由注入的归还端口（``DingTalkAdapter``）
完成，测试注入替身。
"""
from dataclasses import dataclass

from contracts.model import Code, ContractError, Resource, State, aware, require
from contracts.ports import FormRow, check_binding, row_ref

from .gate import assert_business_allowed
from .inbox import referenced_sources

#: 读行失败时说给操作员听的那句话。码本身（``EVIDENCE_REQUIRED``）说的是「证据不够」，
#: 这里把归还这条线上的两种来源说清楚：行读不出，或必填两格缺一。
READ_NOTE = '行读不出，或借用人 / 归还时间缺一（必填缺）'
#: 登记阶段每个原因码一句固定说明。文案是**静态**的：不掺行内容，人名与单据值不进报告。
REGISTER_NOTES = {
    Code.EVIDENCE.value: '定位不到唯一一张借出中的单（名下没有借出中的单，或同一借用人有 ≥2 张：'
                         '需在归还表单填「归还物品」指定单据）',
    Code.STATE.value: '定位到的单已经不在借出中（已被推进或改表），这一行的证据不是判据',
    Code.CONFIG.value: '水位未配置：本轮只出报告、不登记任何归还行（dry-run）',
    Code.WRONG_PERSON.value: '定位到的单不是这一行的借用人名下的，不登记',
}
DUPLICATE_NOTE = '同一行在一次扫描里出现两次，两次都不动'
#: 没配水位时每行都记这条：报告要写明「配了才会自动登记」。
NO_WATERMARK_NOTE = REGISTER_NOTES[Code.CONFIG.value]
#: 扫描内容就地定性这一行的三种结论（判不准时是 ``None``，交给逐行精读）。
_SCANNED_ENTRY = 'entry'
_SCANNED_HISTORY = 'history'
_SCANNED_CONFIG = 'config'


@dataclass(frozen=True)
class RegisteredReturn:
    """一行归还已登记的成果：归还行引用 + 它定位到的那张借出中的单。

    队列以 ``kind='event'`` 的「单据 + 来源」对储存，所以这就是队列写入的最小单位；
    ``kind`` 属性让队列侧不必再判断来源种类（与 ``RegisteredApply`` 同一形态）。
    """

    source: Resource
    loan_ref: Resource

    @property
    def kind(self):
        return 'event'


@dataclass(frozen=True)
class ReturnSkip:
    """一行归还没有进流程，以及为什么。原因码是公共 Code，行 id 是平台记录 id。

    ``note`` 是固定文案（不掺行内容）：同一个码在归还这条线上有几种来源，报告要能
    说出「需指定单据」这类下一步动作，而不是只丢一个码给操作员。
    """

    row_id: str
    code: str
    note: str = ''

    def line(self):
        suffix = f'（{self.note}）' if self.note else ''
        return f'  跳过 归还 row={self.row_id} {self.code}{suffix}'


@dataclass(frozen=True)
class ReturnReport:
    """一轮归还发现的账目：扫描 / 本机已有引用 / 本轮登记 / 跳过 / 历史行 / 非归还行。

    ``scan_code`` 非空表示扫描本身没跑完（读表失败、容器或映射没声明、发现阶段抛异常），
    此时没有任何登记发生 —— 缺表不等于空表。``container`` 是本轮查的结果表，
    ``scanned`` 是这次回读到的行数：0 行且没有 ``scan_code`` 才是「表真的为空」。
    ``history`` 是水位之前的归还行 id（不登记）。``entry_rows`` 是**不是**归还提交的行
    （本机建的阶段入口行、两格都没填的空白行）：它们既不是跳过原因也不是失败，但要
    看见 —— 入口行与本机「本机已有」的行是两回事，混在一起就看不出表里到底有几条归还。
    ``since`` 是本轮用的水位（未配时为空）。
    """

    scanned: int = 0
    known: int = 0
    findings: tuple = ()
    skipped: tuple = ()
    scan_code: str = ''
    scan_note: str = ''
    container: str = ''
    history: tuple = ()
    entry_rows: tuple = ()
    since: str = ''


def failed_return_report(code, note='归还发现扫描抛异常，本轮未登记'):
    """发现阶段没跑完的账目：本轮不登记任何归还，但这一轮照常继续。

    ``scan_code`` 非空即「查表没结论」，报告里与「扫到 0 行、表真的空」区分开。
    """
    return ReturnReport(scan_code=code, scan_note=note,
                        skipped=(ReturnSkip('-', code, ''),))


def format_return_lines(report):
    """报告文本：只有计数、平台记录 id、结果表容器与原因码，不含人名/物品/业务值。

    与申请发现同一形态、**另一行**：两条线各自的扫描 / 水位 / 登记 / 跳过分开算账。
    """
    lines = ['归还发现：扫描 {scanned}，本机已有 {known}，本轮登记 {new}，跳过 {skipped}'.format(
        scanned=report.scanned, known=report.known,
        new=len(report.findings), skipped=len(report.skipped))]
    if report.container:
        verdict = ''
        if not report.scan_code:
            verdict = ('（读表成功，本次回读 0 行 = 结果表当前确实为空，不是读不到）'
                       if report.scanned == 0 else '（读表成功）')
        lines.append('  来源：归还收集表 container={container}，本次回读 {rows} 行{verdict}'.format(
            container=report.container, rows=report.scanned, verdict=verdict))
    if report.scan_code:
        lines.append('  跳过 归还 row=- {code}（{note}，本轮未登记任何归还）'.format(
            code=report.scan_code, note=report.scan_note or '本轮未读到归还收集表'))
    if report.since:
        lines.append(f'  水位：只处理归还时间 ≥ {report.since} 的归还行')
    elif not report.scan_code:
        lines.append(f'  水位未配置：{NO_WATERMARK_NOTE}；'
                     '在绑定里配 return_intake.since 之后才会自动登记新归还')
    if report.history:
        lines.append('  历史行（归还时间在水位之前，不登记）：{ids}'.format(
            ids='，'.join(report.history)))
    if report.entry_rows:
        lines.append('  非归还行（阶段入口行 / 两格都没填，不是归还表单提交）：{count} 行 {ids}'.format(
            count=len(report.entry_rows), ids='，'.join(report.entry_rows)))
    for finding in report.findings:
        lines.append(f'  登记 归还 row={finding.source.resource_id}')
    for skip in report.skipped:
        if report.scan_code and skip.row_id == '-':
            continue  # 扫描本身没结论：上面那行已经写明了，别重复
        lines.append(skip.line())
    return lines


class ReturnIntake:
    """一轮归还发现。所有写入都在绑定与机器租约之下。

    ``since`` 是启用水位（``return_intake.since``，带时区）：只处理归还时间在水位之后的
    归还行；**没配水位就一行都不登记**，只出报告。第一次启用时表里已有的归还行在本机
    没有任何记载（队列、日志都不认识它们），所以「首轮要不要把它们当新归还」本机没有
    判据 —— 一律不登记是刻意的保守默认。

    登记之后不另记本机登记册：这一条线唯一的本机写入就是那条队列引用，它本身就是
    「已登记」的判据（见模块开头）。所以 ``run`` 只回答三件事：扫到几行、哪几行本机
    已经认识、哪几行这轮登记了。
    """

    def __init__(self, engine, port, sources, store, locks, since=None):
        self.engine = engine
        self.port = port
        self.sources = sources
        self.store = store
        self.locks = locks
        if since is not None:
            aware(since)
        self.since = since

    def run(self):
        assert_business_allowed(self.engine.binding, self.engine.lease, self.locks)
        known_ids = self._known_row_ids()
        container = getattr(self.port, 'entry_container', '')
        try:
            rows = self.port.pending_returns(self.engine.binding.account.tenant_id)
        except ContractError as exc:
            if exc.code == Code.INSTANCE:
                raise
            return self._report(container, skipped=(ReturnSkip('-', exc.code.value, ''),),
                                scan_code=exc.code.value)
        scanned = 0
        known = 0
        findings = []
        skipped = []
        history = []
        entry_rows = []
        seen = set()
        for row in rows:
            scanned += 1
            ref = row_ref(row)
            row_id = ref.resource_id
            if row_id in seen:
                # 同一行在一份清单里出现两次：不猜哪一次是真的，两次都不动。
                skipped.append(ReturnSkip(row_id, Code.DUPLICATE.value, DUPLICATE_NOTE))
                continue
            seen.add(row_id)
            if row_id in known_ids:
                known += 1
                continue
            # 扫描内容在手上先就地定性一次：入口行 / 空白行 / 水位之前的行不值得再为它发
            # 一次平台读（归还表里大多数行是这三种）。判不准（端口的扫描行没带单元格、
            # 形态没见过、解码不通过）一律 ``None``，照旧逐行精读。
            scope = self._scanned_scope(row)
            if scope == _SCANNED_ENTRY:
                entry_rows.append(row_id)
                continue
            if scope == _SCANNED_CONFIG:
                skipped.append(ReturnSkip(row_id, Code.CONFIG.value, NO_WATERMARK_NOTE))
                continue
            if scope == _SCANNED_HISTORY:
                history.append(row_id)
                continue
            # 要**动**这一行（登记一条引用）才精读：判据永远来自裸引用上的一次平台读。
            try:
                draft = self.port.read_return(ref)
            except ContractError as exc:
                if exc.code == Code.INSTANCE:
                    raise
                skipped.append(ReturnSkip(row_id, exc.code.value, READ_NOTE))
                continue
            if draft is None:
                # 不是归还表单提交（阶段入口行 / 两格都没填）：连水位都不判。
                entry_rows.append(row_id)
                continue
            if self.since is None:
                # 没有水位就不登记：首次启用不得把表里的历史归还行全推进去。
                skipped.append(ReturnSkip(row_id, Code.CONFIG.value, NO_WATERMARK_NOTE))
                continue
            if draft.occurred_at < self.since:
                # 水位之前的行是启用前就存在的历史归还：不登记、不推待办，单独记账。
                history.append(row_id)
                continue
            try:
                loan_ref = self._register(ref, draft)
            except ContractError as exc:
                if exc.code == Code.INSTANCE:
                    raise
                skipped.append(ReturnSkip(row_id, exc.code.value,
                                          REGISTER_NOTES.get(exc.code.value, '')))
                continue
            findings.append(RegisteredReturn(ref, loan_ref))
        return self._report(container, scanned=scanned, known=known,
                            findings=tuple(findings), skipped=tuple(skipped),
                            history=tuple(history), entry_rows=tuple(entry_rows))

    def _scanned_scope(self, row):
        """扫描内容能不能就地定性这一行（**不发平台调用**）。

        只在端口把**扫描时的单元格**一起给了（``FormRow``）时判，而且只用两样本地事实：
        这一行是不是归还提交（与 :meth:`read_return` 同一套判据）、归还时间在不在水位之后。
        判出来是入口行 / 空白行 / 水位之前 / 水位没配的归还提交，就不必再为它发一次平台
        读 —— 归还表 22 行里省下的就是这一笔（单轮 60~100 秒的大头）。

        判不准一律返回 ``None``：裸引用（替身端口、老接口）、形态没见过、解码不通过都交给
        逐行精读。预筛只会**少做**平台调用，永远不会把「判不准」当成「不用管」。
        """
        if not isinstance(row, FormRow):
            return None
        try:
            draft = self.port.read_return(row)
        except ContractError:
            return None
        if draft is None:
            return _SCANNED_ENTRY
        if self.since is None:
            return _SCANNED_CONFIG
        if draft.occurred_at < self.since:
            return _SCANNED_HISTORY
        return None

    def _report(self, container, **fields):
        return ReturnReport(container=container, since=self._since_text(), **fields)

    def _since_text(self):
        return '' if self.since is None else self.since.isoformat()

    def _register(self, row, draft):
        """定位唯一一张借出中的单，并登记成一条 ``kind='event'`` 引用。

        定位用既有的 ``resolve_return_form_loan``（#77）：**借用人唯一** 或
        **借用人 + 归还物品唯一**，0 张与 ≥2 张一律 fail closed（``EVIDENCE``），
        绝不挑一张 —— 挑错就是把别人的单推成待归还。

        登记前的两道判据与 ``_read_return_form_event`` 执行侧同源（借用人必须就是这一行
        的人、单必须还在借出中）：登记侧先用同一口径筛一遍，宁可少登记一条，也不往队列里
        塞一条注定被拒的行 —— 那种行每轮都会在报告里报一次跳过，看不出是配置错了。
        """
        loan_ref = self._resolve(row)
        if loan_ref is None:
            # 解析侧不认这一行（不是归还提交 / 没声明归还表单映射）：不猜、不登记。
            raise ContractError(Code.EVIDENCE)
        loan = self.engine.reader.read_loan(loan_ref)
        check_binding(self.engine.binding, loan)
        require(loan.borrower == draft.borrower, Code.WRONG_PERSON)
        require(loan.state == State.BORROWED, Code.STATE)
        append = getattr(self.sources, 'append', None)
        if append is not None:
            # 队列条目是这一行唯一的本机凭据，也是「已登记」的判据：只追加，去重靠键。
            append((RegisteredReturn(row, loan_ref),))
        return loan_ref

    def _resolve(self, row):
        """用既有的 ``resolve_return_form_loan`` 定位唯一一张借出中的单。

        ``hint_ref`` 只被解析侧当作台账表容器的兜底（``self.loan_container or
        hint_ref.container_id``）。这里先确认适配器声明过 ``loan_container``，免得它
        退回拿**归还行所在的表**去匹配台账 —— 那会匹配到另一张表，还可能「恰好唯一」。
        确认过之后兜底分支不会走到，所以直接传行本身，不另造一个假单据引用。
        """
        container = getattr(self.port, 'loan_container', '')
        require(_declared(container), Code.CONFIG)
        return self.port.resolve_return_form_loan(row, row)

    def _known_row_ids(self):
        """本机任何来源引用里已经出现过的归还行 id（幂等判据）。

        只认**归还所在的这张表**里的 form 引用：别的表里恰好同号的记录不能替一行归还
        免登记。两个来源都算：工作队列条目、操作日志里的事件来源与阶段回执来源。
        """
        container = getattr(self.port, 'entry_container', '')
        known = set()
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


def _declared(value):
    """这一格在这份绑定里声明过没有（不是空串、不是 ``unset:`` 哨兵）。"""
    return (isinstance(value, str) and bool(value.strip())
            and not value.startswith('unset:'))
