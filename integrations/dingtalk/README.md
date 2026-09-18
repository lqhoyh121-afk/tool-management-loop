# 钉钉读写适配器（T03 · 连接层）

Refs #3。在冻结的 `contracts/` 之上实现 ReadPort / WritePort / StagePort。传输对象由调用方注入；本目录**不**读凭据、不调用真实 dws、不断言 L4。

离线解析（报文封套、单元格、待办详情、人员命名空间）仍按公开 T01 报告的已观察形态工作。连接层把这些解析接到公共契约：精确读借用/库存/可信事件，发受限表单或独立待办，提交后必须按原意图回查。

## 模块

| 文件 | 职责 |
|---|---|
| `errors.py` | 解析层失败信号。映射到契约时用 `contracts.model.ContractError`。 |
| `identity.py` | 带来源命名空间的人员标识，不做通用跨命名空间转换。 |
| `envelope.py` | 报文封套校验与记录列表提取。缺 `success` 不能当成功；`data.records` 与顶层 `records` 并存视为歧义。 |
| `cells.py` | 多维表单元格取值与形态校验。 |
| `todo.py` | 待办详情与实际完成事件。`finish_time` 保留原始毫秒整数；`completion_at` 唯一换算为 Asia/Shanghai datetime。 |
| `layout.py` | 适配器私有字段 ID，不是公共契约名。台账用 `FieldMap`，阶段入口用 `EntryFieldMap`；重叠键在两张表上是不同 ID，禁止共用一套映射。 |
| `codec.py` | Loan / Inventory 按 T01 单元格类型编解码。角色字段写成 creator 形态 `[{corpId,userId}]`，读出后作为 **contact** Identity；这是本适配器写入后再读回的约定，不是通用 record_creator→contact 转换。 |
| `transport.py` | 注入传输接口。`None` 表示超时/掉线，结果未知。 |
| `dws_transport.py` | 真实 dws CLI 驱动。调用方注入 `node`+`dws.js`（或测试假脚本）；不读凭据、不猜安装路径。`--records-file` 只传 Windows 原生路径。`form.create` 是向**已有**收集结果表 `record create`，不是 `view create`。`stage.query` 只读本机阶段索引。 |
| `adapter.py` | Read/Write/Stage 端口。每次写入检查 lease 与 `check_binding`；发前将回执标为 unknown；不明结果只 query，不盲重发。 |
| `application.py` | 申请行 → 台账行的发现形态：`ApplicationDraft` 与链路键 `application_marker`（台账行「申请证据」格里的 `form:<申请行 id>`）。发现、建行、认领都靠这一个键，不建第二本登记。 |

## 注入命令名（仅测试/适配器内部）

真实 T07 必须把这些名字对到本机已验证的 dws/多维表命令，不能把本表当作已证明的平台 API。

| 内部命令 | 对应的 T01 观察 / 停止点 |
|---|---|
| `record.query` | `record query`（`data.records`）与 `record query --all`（顶层 `records`） |
| `record.update` | T07：`record update --base-id --table-id --records-file <Windows 原生路径> --yes --format json`；失败形态已知 `SELECT_OPTION_NOT_FOUND`（写自造选项 id，见 #30） |
| `form.create` / 表单记录查询 | T01 用的是受限收集表（`view get` 的 formInfo + 结果表 `record query`），不是 OA。建表/授权若只能靠 UI，程序不得假装 CLI 已可建入口 |
| `todo.create` | `todo task create --executors` 接受通讯录 userId |
| `todo.get` | `todo task get` → `result.todoDetailModel` |
| `stage.query` | 适配器保存的原单/阶段绑定回读；平台没有同名命令 |
| `application.list` | `record query --all`（无行 id、无过滤条件）：列整张申请表结果表，只回行 id 与单元格；空表回 `records: null` |
| `loan.create` | `record create`（台账借用单表）：申请自动建行，写后必须精确回读，回读不通过即 `WRITE_UNKNOWN_QUERY_FIRST` |
| `loan.find_application` | `record query --filters`（`申请证据` eq `form:<申请行 id>`，带 `--all`）：写入结果不明时按链路键认领，命中 0 条为「没有」，多条按歧义 fail closed |

## 字段映射（T01）

台账记录走 `FieldMap`（`codec.decode_loan` / `encode_loan` / 库存）。申请收集表走 `ApplicationFieldMap`；阶段入口走 `EntryFieldMap`（`adapter._read_form_event` 按 `source.container_id` 分流；`form.create` 仍写阶段入口）。`config_version`、`quantity`、`physical_ids`、`borrower`、`approver`、`manager`、`return_container`、`return_id` 等在多表上是不同字段 ID；缺任一套映射是 CONFIG，不得把台账 `fields` 套到入口或申请表。真实 ID 只存在本机 `binding.json`，合成夹具不得冒充生产字段。阶段入口「决定」另认 `request_return`/`归还`/`拒绝` 等现场选项名，以及引擎写入的 `action` 文本字段。singleSelect **写**只发选项 name 字符串；**读**只接受 `{id, name}` 对象，业务值取 `.name`（`.id` 是服务端随机串，不得回传合成 id）。空的 `return_id` / `return_container` 钉钉不回传，解码按空字符串，不得当缺证失败；反过来，出现的**空字符串**不是已观察形态，按缺字段拒绝。隔离替身读侧按 state 里声明的 `kinds` 物化读回形态（select `{id, name}`、person `[{corpId, userId}]`、number 字符串），`id` 与 `name` 相同的自造选项按未观察形态拒绝。

- number：字符串，显式解析为有限小数后再收窄为整数。
- date：带时区 ISO 字符串。
- 角色/creator：`[{corpId,userId}]`。
- 待办内部人员 ID、`finishTime`：int；内部 ID 规范为十进制字符串后放入 `Identity(namespace="todo")`。
- 完成事件：只认 `task.self.done` / `task.done`；同一 task 上两者归一为一条业务事件，不看成两次确认。
- **`finishTime` 为毫秒整数**（T07 真机核对）。连接层经 `completion_at` 换算为 Asia/Shanghai 带时区 datetime 写入 `Event.occurred_at`；展示用 `YYYY-MM-DD HH:mm`。真机 `todo task get` 无 `result.occurredAt`，不得依赖或伪造该字段。

## 写入与未知结果

1. `prepare` 原意图，同 ID 不同负载拒绝。
2. 发前保存 unknown。
3. 写超时、限流：outcome=unknown。回查时若借用和库存都仍是发前快照，记 `NOT_SENT`，允许按原 `operation_id` 再 submit。部分目标已变仍是 unknown，不重放整个意图。
4. `FORBIDDEN` / `PERMISSION_DENIED` → `IDENTITY_REQUIRED`，立刻停止。
5. 部分写入（借用已改、库存未改）保持 unknown，不重放整个意图，不把缺查询当成 not_applied。
6. **`query()` 在记录根本不存在或读失败时仍返回 UNKNOWN，不把“查不到记录”编成 NOT_APPLIED。** 两个目标都还停在发前快照时才是 `NOT_SENT`。
7. 阶段回查的容器 ID 只取 `stage.query` 结果里的 `container` 字段；缺字段、空字符串或非字符串按未观察形态失败。不得写死测试夹具名 `synthetic-forms` / `synthetic-todos`。

阶段入口同样：ISSUE/RETURN 为两条独立待办并保存 `IdentityBinding`；APPROVE 表单同时承载同意/拒绝；创建超时只回查，不重创建。

## T07 主控实测命令与停止点

隔离测试通过 **不等于** L4。下列命令仅供主控本机对照，协作者不得索要凭据或代跑真实组织。

建议先只读：

```text
dws auth status --format json
dws contact user me -y
dws record query
dws record query --all
dws todo task get
```

写入前必须能精确指出目标 Base/表/记录/待办，并先回读。候选写命令（以本机帮助与 T01 回执为准，不在此编造参数）：

```text
node <injected-dws.js> aitable record query --base-id <ID> --table-id <ID> --record-ids <ID> --format json
node <injected-dws.js> aitable record query --base-id <ID> --table-id <ID> --record-ids <ID> --all --format json
node <injected-dws.js> aitable record update --records-file <Windows-native-path> --yes --format json
node <injected-dws.js> aitable record create --records-file <Windows-native-path> --yes --format json
node <injected-dws.js> todo task create --executors <contact-userId> --yes --format json
node <injected-dws.js> todo task get --task-id <ID> --format json
node <injected-dws.js> chat message send --user <userId> --title <title> --text <text> --yes --format json
```

**停止点（任一出现即停，不盲发、不声称成功）：**

1. 受限表单无法证明“原单完整引用 + 指定人 + 明确决定”，只靠可编辑标题或说明文本。
2. 待办完成活动的 creatorId 无法与创建时通讯录执行者做成 **该 task 限定** 的 IdentityBinding。
3. 待办已完成但 `finishTime` 为 0 或缺失，无法换算完成时刻。
4. `success: true` 同时带 `error.code`，或 `view update` 一类“受理但未改变配置”的回执。
5. 权限不足、限流、超时、只回读到其中一个目标。
6. 普通人主账未整体拒绝、或字段级权限未验证却当成已隔离。
7. 需要跨机器、并发、OA 审批流或通用审批关联——首版不支持。

8. 收集表 Form 视图 CLI 创建返回 `UNSUPPORTED_VIEW_TYPE`：不得改走 `view create`；只允许对主控已建好的结果表做 `record create`。
9. `--records-file` 不是 Windows 原生绝对路径。

所需权限（T01 已部分证明、T07 仍须本机重验）：当前运行账号可读写指定主账；普通人不可访问该主账；审批/归还为仅指定人可填的受限收集表；借出/归还确认待办只分配给指定管理人。

## 依据的已观察形态

下表每行都能在公开报告里找到对应描述；**报告没写的形态一律报错，不猜**。

| 形态 | 报告记录 | 本目录的处理 |
|---|---|---|
| `record query` 单页 | `data.records[].recordId` 与 `cells[fieldId]` | `envelope.extract_records` 优先读 `data.records` |
| `record query --all` | 有数据时是顶层 `records` | 同一函数回退读顶层 `records` |
| `--all` 空表 | 曾返回 `records: null, hasMore: false, pages: 1` | `null` 且 `hasMore is False` 时当空结果；缺 `hasMore` 或其它假值报错 |
| `--all` + `--filters` 无命中 | 同样是 `records: null`（键在、值为 null），不是 `[]` | 按空结果处理；`--filters` 对 singleSelect 按**选项 name** 比较 |
| 成功封套 | 主控核对原始回执：`status=success` 且 `error={}`；正式段要求 `success` 键存在且为 true | 空 `error` 表示无业务错误；缺 `success`/`status`/`error` 不能当成功 |
| 顶层成功掩盖业务错误 | Base 不存在时 `success: true` 同时 `status: error`、`error.code=BASE_NOT_FOUND` | 先查 `error.code` 与 `status`，不把 success 布尔值当成功 |
| 两种 records 同时出现 | 正式段收紧：不得静默取 `data` | `UnsupportedShapeError` |
| 未支持错误封套 | aitable 通道：公开报告未确认顶层 `errorCode` / `errorMsg` | aitable 即使带空 `records` 也报错；todo 通道封套见 #34 |
| singleSelect 写 | T07 #30：`record update` 只接受选项 name 字符串 | `encode_loan` 写 name；写 synthetic option id 返回 `SELECT_OPTION_NOT_FOUND` |
| singleSelect 读 | T01/T07：`{id, name}` 对象，业务值在 `.name`，`.id` 是服务端随机串 | `decode_loan` 与阶段决定只读 `{id, name}`；裸字符串报错；`id` 与 `name` 相同按自造形态报错 |
| 隔离替身的读回 | 替身存的是**写入**载荷，真机读回是物化后的单元格 | 两个替身共用 `t03_live_cells.py`：按 state 里的 `kinds` 物化 select/person/number；未声明字段原样透传，让解码 fail closed |
| 未填单元格 | 钉钉不回传未填字段（如空 `return_id` / `return_container`） | 缺字段或 `null` 算「可选且为空」；出现的**空字符串**按缺字段报错，不猜 |
| creator 单元格 | `[{corpId, userId}]`，组织加人员的二元身份；不能用姓名代替 | 解析为 `record_creator`；codec 在本适配器自写自读的角色字段上收成 contact Identity |
| number 单元格 | 本次回读为字符串，需显式数值解析 | 只接受字符串，解析为有限 `Decimal` |
| date 单元格 | 带时区 ISO 字符串 | 解析为 aware `datetime`；无时区报错 |
| 待办人员 ID | `executorIds`、`activities[].creatorId` 为 int | `todo` 命名空间；int 转规范十进制字符串 |
| 实际完成事件 | `task.self.done`、`task.done`；`finishTime` 为 int（毫秒） | 完成活动只认这两种 action；`completion_at` 唯一换算 |

## 人员标识命名空间

- `contact`：通讯录接口返回、且被待办创建参数接受的 userId。
- `todo`：待办详情内部的人员 ID。
- `record_creator`：多维表 creator 单元格中的 userId，与 corpId 成对。

`PersonRef.__eq__` 与 `same_person_as` 都包含命名空间和组织。跨命名空间比较报错而不是静默 False。IdentityBinding 只按 **该次创建参数 + 该 task 回读 + 新完成活动** 建立，不缓存成通用换算表。

## 未验证项

1. 真实 `record update` 的 CAS/revision 语义；`SELECT_OPTION_NOT_FOUND` 已在 T07 #30 观测（写 synthetic option id 被拒），其余失败形态仍待补帧。
2. 程序化创建受限表单是否可代替 T01 的 UI 配置。
3. `record_creator` 与通讯录 userId 是否在任意人员上恒等（本适配器只依赖自己写入的角色单元格）。
4. 跨创建者待办内部 ID 映射（T01 未验）。
5. 字段级权限、OA、并发、断网重启、跨机器。
6. 真实组织 L4。本目录的 unittest 只覆盖注入传输。

## 测试

合成夹具：`tests/integrations/fixtures/t01_observed_shapes.json`（解析形态）与 `tests/contracts/fixtures.py`（契约对象）。字符串标识用 `SYNTHETIC-` 前缀；待办内部整数 ID 落在 `9000000000` 及以上。注入传输是 `t03_memory_transport.py`，不用通用名 `support`。两个替身（内存传输与 `t031_fake_dws.py` 假 CLI）的读回形态共用 `t03_live_cells.py`：字段类型由 state 的 `kinds` 声明，不靠字段白名单，也不靠替身猜 `'awaiting_approval'` 这种裸字符串。

```text
python -B scripts/repo_checks.py
python -B -m unittest discover -s tests/integrations -p "test_*.py" -v
```

不要给 `tests/integrations/` 加 `__init__.py`，以免遮蔽顶层 `integrations/` 包。
