"""申请收集表行 → 台账行的发现/登记形态（适配器层，非公共契约）。

申请表结果表是申请的真源；台账行是本机流程的载体。两者之间只认一个**本机链路键**：
台账行的 `application_evidence` 格里写入的 `form:<申请行 id>`（与
`contracts.flow.accept_application` 写入的值同源）。它让「这条申请是否已经建过台账行」
变成一次精确回读，而不是第二本登记册或近似匹配。

本模块只放数据形态与校验，不发任何平台请求；读写都由适配器完成。
"""
from dataclasses import dataclass
from datetime import datetime

from contracts.model import (Code, Identity, Loan, Resource, State, require, text)

# 申请行 → 台账行的链路键前缀。form 是申请入口的来源种类（与读侧
# `_read_application_event` 的 `evidence_ref` 一致）。
MARKER_PREFIX = 'form:'


def application_marker(source: Resource) -> str:
    """申请行对应的链路键；只含平台记录 id，不含人名或业务单号。"""
    text(source.resource_id)
    require(source.kind == 'form', Code.EVIDENCE)
    return f'{MARKER_PREFIX}{source.resource_id}'


@dataclass(frozen=True)
class ApplicationDraft:
    """一行已校验的申请，还没变成台账行。

    必填项齐全才构造得出来：缺项一律在读取处 fail closed，不在这里补默认值。
    逐件管理（`tracked`）由申请行是否给出实物编号决定 —— 申请行没有第三个状态可猜。
    """

    source: Resource
    item: Resource
    borrower: Identity
    quantity: int
    physical_ids: tuple
    occurred_at: datetime
    due_at: datetime

    def __post_init__(self):
        require(self.source.kind == 'form', Code.EVIDENCE)
        require(self.item.kind == 'record', Code.EVIDENCE)
        require(self.borrower.namespace == 'contact', Code.IDENTITY)
        require(self.borrower.tenant_id == self.source.tenant_id, Code.IDENTITY)
        require(self.item.tenant_id == self.source.tenant_id, Code.IDENTITY)
        require(isinstance(self.quantity, int) and not isinstance(self.quantity, bool)
                and self.quantity >= 1, Code.QUANTITY)
        require(isinstance(self.physical_ids, tuple), Code.EVIDENCE)
        for identifier in self.physical_ids:
            require(isinstance(identifier, str) and bool(identifier.strip())
                    and identifier == identifier.strip(), Code.IDENTIFIERS)
        require(len(set(self.physical_ids)) == len(self.physical_ids), Code.IDENTIFIERS)
        # 逐件管理由申请行的实物编号决定：要么逐件给全，要么按数量管理。
        require(len(self.physical_ids) == (self.quantity if self.physical_ids else 0),
                Code.IDENTIFIERS)
        require(self.due_at > self.occurred_at, Code.INVALID)

    @property
    def marker(self):
        return application_marker(self.source)

    @property
    def tracked(self):
        return bool(self.physical_ids)

    def loan(self, ref, binding):
        """待写入的台账行：批准人/管理人/配置版本只来自绑定，不来自申请行。"""
        return Loan(
            ref, self.item, self.borrower, binding.approver, binding.manager,
            self.quantity, self.tracked, tuple(self.physical_ids),
            self.due_at, binding.config_version, State.AWAITING_APPROVAL,
            None, (), self.marker,
        )

    def pending_ref(self, container_id):
        """建行前的占位引用；只用于绑定校验，绝不落盘、绝不外发。"""
        text(container_id)
        return Resource('record', self.source.tenant_id, container_id, 'pending')
