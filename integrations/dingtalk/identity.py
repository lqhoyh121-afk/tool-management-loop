"""带命名空间标记的人员标识。

公开 T01 报告记录了三处来源不同的人员标识，报告同时说明不得把它们直接相等
比较。本模块只保留"这个标识是从哪儿读出来的"，不做任何转换，也不替 T02 定义
公共身份类型。

- ``contact``：通讯录接口返回、且被待办创建参数接受的 userId。
- ``todo``：待办详情内部的人员 ID（executorIds、activities[].creatorId）。
- ``record_creator``：多维表 creator 单元格中的 userId，与 corpId 成对出现。

``record_creator`` 必须带组织。同命名空间比较同时核对组织；跨组织同人员取值
不是同一个人。T01 只在同一个人身上观察到 ``record_creator`` 与 ``contact``
取值一致，报告未把它确认为通用映射，因此本模块不提供跨命名空间转换。
"""
from dataclasses import dataclass

from .errors import IdentityNamespaceError

CONTACT = 'contact'
TODO = 'todo'
RECORD_CREATOR = 'record_creator'

NAMESPACES = (CONTACT, TODO, RECORD_CREATOR)


@dataclass(frozen=True)
class PersonRef:
    """一个人员标识及其来源命名空间。

    ``==`` 比较包含命名空间与组织，所以通讯录标识永远不等于待办内部标识。
    要回答"是不是同一个人"请用 :meth:`same_person_as`，跨命名空间时它会报错
    而不是静默给出 False。
    """

    namespace: str
    value: str
    org: str | None = None

    def __post_init__(self):
        if self.namespace not in NAMESPACES:
            raise IdentityNamespaceError(f'未知人员命名空间: {self.namespace!r}')
        if not isinstance(self.value, str) or not self.value:
            raise IdentityNamespaceError('人员标识必须是非空字符串')
        if self.namespace == RECORD_CREATOR:
            if not isinstance(self.org, str) or not self.org:
                raise IdentityNamespaceError('record_creator 必须带组织标识')
        elif self.org is not None:
            raise IdentityNamespaceError(
                f'{self.namespace} 未观察到组织标识，不能填写 org'
            )

    def require(self, namespace):
        """确认本标识来自 ``namespace``，否则报错。"""
        if self.namespace != namespace:
            raise IdentityNamespaceError(
                f'需要 {namespace} 命名空间的人员标识，收到 {self.namespace}'
            )
        return self

    def same_person_as(self, other):
        if not isinstance(other, PersonRef):
            raise IdentityNamespaceError('只能与 PersonRef 比较')
        if self.namespace != other.namespace:
            raise IdentityNamespaceError(
                f'{self.namespace} 与 {other.namespace} 是不同命名空间，'
                '公开报告未确认两者可直接互换'
            )
        return self.value == other.value and self.org == other.org
