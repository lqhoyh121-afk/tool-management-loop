# 钉钉读写适配器（T03 · 第一段）

Refs #3。当前只有**离线解析**：按公开 T01 报告已观察到的返回形态提取字段、识别错误、区分人员标识命名空间。

**没有**平台调用、端点定义、凭据读取、待办发送、库存写回、身份决策或重试调度。正式连接层等 T02 (#2) 冻结公共契约后再接，那时本目录按契约改写。

## 模块

| 文件 | 职责 |
|---|---|
| `errors.py` | 解析层失败信号。不是公共业务错误码。 |
| `identity.py` | 带来源命名空间的人员标识，不做跨命名空间转换。 |
| `envelope.py` | 报文封套校验与记录列表提取。 |
| `cells.py` | 多维表单元格取值与形态校验。 |
| `todo.py` | 待办详情与实际完成事件。 |

## 依据的已观察形态

下表每行都能在公开报告里找到对应描述；**报告没写的形态一律报错，不猜**。

| 形态 | 报告记录 | 本目录的处理 |
|---|---|---|
| `record query` 单页 | `data.records[].recordId` 与 `cells[fieldId]` | `envelope.extract_records` 优先读 `data.records` |
| `record query --all` | 有数据时是顶层 `records` | 同一函数回退读顶层 `records` |
| `--all` 空表 | 曾返回 `records: null, hasMore: false, pages: 1` | `null` 且 `hasMore` 为假时当空结果；`hasMore` 为真则报错 |
| 顶层成功掩盖业务错误 | Base 不存在时 `success: true` 同时 `status: error`、`error.code=BASE_NOT_FOUND` | 先查 `error.code` 与 `status`，不把 `success` 布尔值当成功 |
| creator 单元格 | `[{corpId, userId}]`，组织加人员的二元身份；不能用姓名代替 | 返回 `record_creator` 命名空间的 `PersonRef`，字符串取值直接报错 |
| number 单元格 | 本次回读为字符串，需显式数值解析 | 解析为 `Decimal`；布尔值、空串、带单位的文本都报错 |
| date 单元格 | 带时区 ISO 字符串 | 解析为 aware `datetime`；无时区报错，不补默认时区 |
| singleSelect 单元格 | `{id, name}` | 原样返回 `SelectOption`，缺 id 或 name 报错 |
| 待办详情 | `todo task get` 返回 `result.todoDetailModel` | `todo.read_todo_detail` |
| 待办人员 ID | `executorIds`、`activities[].creatorId` 是待办内部人员 ID，与通讯录 userId 分属两个命名空间 | 统一标为 `todo` 命名空间；跨命名空间比较报错 |
| 实际完成事件 | `action` 为 `task.self.done`、`task.done`，带 `activityId`、`creatorId`；完成时间在 `finishTime` | `completion_events` 只认这两种 action |
| 完成者判定 | 不能只依据执行人或 `modifierId`；勾完成也不等于同意 | `completed_by` 只看完成活动的 `creatorId`，完全不读 `isDone`、`modifierId` |

## 人员标识命名空间

报告观察到三处来源，并明确不得把它们直接相等比较：

- `contact`：通讯录接口返回、且被待办创建参数接受的 userId。
- `todo`：待办详情内部的人员 ID。
- `record_creator`：多维表 creator 单元格中的 userId，与 corpId 成对。

`PersonRef.__eq__` 包含命名空间，所以通讯录标识永远不等于待办内部标识。要回答"是不是同一个人"用 `same_person_as`，跨命名空间时它报错而不是静默返回 False。

报告只在同一个人身上观察到 `record_creator` 与 `contact` 取值一致，并未确认为通用映射，所以本目录**不提供**任何跨命名空间转换。

## 结果未知

没有响应报文（例如超时）时 `read_envelope(None)` 抛 `UnknownResultError`，语义是"结果未知"，不是失败。调用方必须先按精确目标回查再决定后续，禁止盲目重发。

回查与重试调度本身**不在本段实现**：它们依赖 T02 冻结的操作标识与接口语义。

## 未验证 / 留给 T02 的问题

1. 报告只逐字记录了 `success` / `status` / `error.code` 三个封套字段。其他错误封套形式（例如命令行常见的 `errorCode` / `errorMsg`）未在公开报告中确认，本目录不处理，遇到即报错。
2. `record_creator` 的 userId 与通讯录 userId 是否恒等，未确认。
3. number 是否可能返回非字符串、date 是否可能返回时间戳，均未观察到，当前报错。
4. 单元格 creator 出现多个的形态未观察到，当前报错。
5. `task.self.done` / `task.done` 之外的完成类事件未观察到。
6. 正文说明中的原单引用是受控配置关联，**不是不可篡改外键**，不能当唯一业务主键用。
7. 公共字段名、状态机、错误码、幂等标识与客户端接口都属于 T02；本目录的异常类与 `PersonRef` 只是本层解析工具，契约冻结后按契约改写或删除。

## 测试

合成夹具在 `tests/integrations/fixtures/t01_observed_shapes.json`，每份样例标注来源报告条目与模拟性质，标识一律 `SYNTHETIC-` 前缀。没有真实资源 ID、人员、台账内容或个人路径。

统一入口（T09 已合入 main）：

```text
python scripts/repo_checks.py
```

显式目录入口：

```text
python -m unittest discover -s tests/integrations -p "test_*.py" -v
```

辅助模块是 `t03_fixture_loader.py`，不用通用名 `support`。各测试按本文件所在目录把它加入搜索路径，因此统一入口按文件导入时也能加载，不必改 CI 或全局 `PYTHONPATH`。不要给 `tests/integrations/` 加 `__init__.py`，以免遮蔽顶层 `integrations/` 包。
