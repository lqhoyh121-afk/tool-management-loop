"""归还收集表行 → 队列引用的发现形态（适配器层，非公共契约）。

归还表单与阶段入口行**共用同一张结果表**（绑定的 ``form_container``），而且两边的
「借用人」「归还时间」两格就是同一列。能把归还提交与本机建的入口行分开的只有「台账单据
指向」那两格（``entry_fields.loan_container`` / ``entry_fields.loan_id``）：入口行一定有，
归还提交一定没有 —— ``_is_return_form_submission`` 用的就是这条判据。

本模块只放数据形态与校验，不发任何平台请求：扫描与逐行读取都由适配器完成。发现侧
（``bootstrap.returns``）拿到的是一份已校验的行，幂等、水位与登记由它自己判断。
"""
from dataclasses import dataclass
from datetime import datetime

from contracts.model import Code, Identity, Resource, require


@dataclass(frozen=True)
class ReturnDraft:
    """一行已校验的归还提交，还没变成队列引用。

    必填两格（借用人 + 归还时间）齐全才构造得出来：缺项一律在读取处 fail closed，
    不在这里补默认值。「归还物品」是可选格，匹配时由 ``resolve_return_form_loan``
    自己按需再读，不经过本类型。
    """

    source: Resource
    borrower: Identity
    occurred_at: datetime

    def __post_init__(self):
        require(self.source.kind == 'form', Code.EVIDENCE)
        require(self.borrower.namespace == 'contact', Code.IDENTITY)
        require(self.borrower.tenant_id == self.source.tenant_id, Code.IDENTITY)
        require(isinstance(self.occurred_at, datetime)
                and self.occurred_at.utcoffset() is not None, Code.EVIDENCE)
