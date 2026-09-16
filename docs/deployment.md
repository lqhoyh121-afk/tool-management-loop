# 部署向导（T04）

当前仓库没有完整业务程序。本说明覆盖可见启动、环境检查、台账只读预览，以及冻结契约后的 **RuntimeBinding / SingleInstance 闸门** 与导入确认回读。真实本机部署与钉钉联调属于 T07，隔离测试通过不能当成 L4。

## 启动

在仓库根目录双击 `bootstrap/start.bat`，或在仓库根目录执行：

```text
python -m bootstrap
```

启动过程使用前台控制台。关闭窗口即结束该进程；入口不会用分离方式拉起后台业务进程，也不会安装全局依赖、写入开机启动或修改系统设置。

仅检查环境与绑定：

```text
python -m bootstrap --check-env --runtime 运行目录
```

仅预览文件（不弹出选择框，仍不写入钉钉）：

```text
python -m bootstrap --preview 路径/到/合成文件.xls
```

生产启动应加 `--require-ready`：绑定或环境未通过时拒绝预览和交互。`runtime/ready.json` **不能**放行。

```text
python -m bootstrap --require-ready --runtime 运行目录
```

失败时窗口保持打开并打印原因，避免双击后立即闪退。

## 绑定与闸门

绑定文件是运行目录下的 `binding.json`（默认仓库 `runtime/`，可用 `--runtime` 或 `TOOL_LOOP_RUNTIME` 覆盖；该目录已 Git 忽略）。须同时满足：

- 运行账号、审批人、管理人为 `contact` 身份
- 主账为 `LedgerScope`（租户 + 容器键），不是单条物资 recordId
- 四项核验全部为字面 `true`：普通人主账拒绝、受限入口、管理账号读写、本人确认
- `application_entry` 指向已有申请入口（form 或 record），T04 不创建第二份借用申请

重复部署遇到已有 `binding.json` 会拒绝覆盖。若只有 `binding.json.tmp`，视为上次中断，正式配置未写入。

机器排他锁按 `lease_key(scope)=tenant_id::container_key` 落在与运行目录无关的锁根（默认 `%PROGRAMDATA%\tool-management-loop\locks`，测试用 `--lock-root` / `TOOL_LOOP_LOCK_ROOT`）。第二实例、另一运行目录或另一账号抢同一主账都会 `SECOND_INSTANCE_BLOCKED`。崩溃留下的锁目录不会 TTL 抢占，须主控确认后手工删除。

业务写入入口调用 `bootstrap.gate.assert_business_allowed`。绕过 BAT 直接 import 该函数同样要绑定和 lease，不看 ready 文件。

保存绑定（测试注入 JSON，不代选真人）：

```text
python -m bootstrap --runtime 运行目录 --bind 合成绑定.json
```

## 导入确认

预览仍只展示原始表头和单元格，不把表头猜成业务字段。确认导入会 **再读一遍** 同一文件，要求首行含契约字段 `available`、`reserved`、`borrowed`、`revision`；缺列直接失败，不编造。回执只保存源 **文件名**、摘要和表头，不保存个人绝对路径，也 **不写钉钉**。同一运行目录已有回执则拒绝重复导入。

```text
python -m bootstrap --runtime 运行目录 --lock-root 锁目录 --confirm-import 路径/到/合成文件.xls
```

## 申请入口（钉钉收集表）配置

`binding.json` 的 `application_entry` 必须指向**已存在的受限收集表（或其表单视图）的精确标识**，不新建第二份申请入口，也不靠可编辑标题猜测目标。

钉钉开放接口不提供创建 `form` 类型视图的能力（`aitable view create` 传 form 类型会被服务端拒绝），因此收集表只能在官方网页端建立，建好后取标识写入绑定。

### 网页端建立

1. 打开目标多维表 Base，侧栏选择「收集表」，新建；会生成一份空白表单，自带一道默认标题题。
2. 重命名表单为业务名称。
3. 点击组件面板中的题型即可追加题目，不需要拖拽。按申请语义逐题配置题名、类型与选项（如单选 + 数字 + 日期），业务字段设为必填。
4. 删除自动生成的默认标题题：题行的操作按钮中有删除项，确认「移除题目」。
5. 「谁可以填写」选**仅指定人可填写**，勾选实际申请人；**匿名填写关闭**；**允许提交后修改关闭**。
6. 发布表单，复制分享链接。

收集表的结果**不落在手工创建的表里**：钉钉会自动生成一张独立结果表，表名与收集表同名，其表 ID 与表单视图 ID 不同。申请真源是这张结果表，回读时必须按结果表 ID 精确查询，不要按近似表名匹配。

各题必填在网页端逐项打开字段容器后设置；接口返回成功不等于配置生效，一律以独立回读为准。

### 日期字段粒度

日期题的「显示格式」继承结果表字段的 `config.formatter`。默认值为纯日期 `YYYY-MM-DD`，填写人只能选到天，落库时间的时分秒为 `00:00:00`。约定归还时间需要精确到分钟时必须改字段配置：

```text
dws aitable field get    --base-id <base-id> --table-id <结果表 id>
dws aitable field update --base-id <base-id> --table-id <结果表 id> --field-id <字段 id> \
  --config '{"formatter":"YYYY-MM-DD HH:mm"}' -y
```

改完以回读为准：`config.formatter` 应为 `YYYY-MM-DD HH:mm`，表单设计器中该题的显示格式随之变为含时分的示例值，填写预览的日期控件出现时、分选择。提交后回读记录，日期值应带完整时分与时区。

本机实测中表单侧「显示格式」下拉未能打开，界面修改路径不可靠；**以 CLI `field update` 加独立回读为准**。

### 发布与核验

发布后用 CLI 回读表单视图信息，与页面显示对照：分享已开启、视图状态为已发布、`authTypeCode` 对应「仅指定人」。受理响应不能替代核验，必须独立回读。

```text
dws aitable view get --base-id <base-id> --table-id <结果表 id> --view-ids <表单视图 id>
```

收集表配置一次即可长期使用，选项随后续台账维护；不需要为每次申请重建表单。正式环境的收集表按本节重做一遍即可，不是重新开发。

## 审批入口（钉钉收集表）配置

审批入口与申请入口分离：在同一个 Base 内另建一张收集表承载审批决定，不新增第二份申请入口，也不把审批结论写回申请结果表。首版审批只需同意/拒绝。

### 题目与选项

1. 侧栏「收集表」新建并重命名；自动生成的默认标题题按申请入口同法移除（选中题行 → 行内最右图标按钮 → 确认「移除题目」）。
2. 追加「审批结果」单选题，另加一道「审批意见」文本题。
3. 新题目默认必填。不需要必填的题，选中题行后用行内的必填开关关闭。

选项名不能只在表单设计器里改：界面输入的选项文本不会同步到服务端，落库仍是「选项一/选项二」。必须用 CLI 按字段 ID 覆写，保留原选项 ID、只改名称：

```text
dws aitable field update --base-id <base-id> --table-id <结果表 id> --field-id <审批结果字段 id> \
  --config '{"options":[{"id":"<选项一 id>","name":"同意"},{"id":"<选项二 id>","name":"拒绝"}]}' -y
```

本机实测中结果表不接受接口新建文本字段（`field create --type text` 报主键列冲突），文本题在表单设计器里追加；单纯改字段名可用 `field update --name`。

### 填写权限

「谁可以填写」选**仅指定人可填写**，指定人即审批人。钉钉界面把这组人称作「必填人」：入口是「设置指定人」下方的「选择必填人」按钮，选人后按钮变为「管理」，列表清空后回到「选择必填人」——按钮文案就是列表是否为空的判据。匿名填写关闭，提交后修改关闭。

### 发布与核验

发布后 CLI 回读表单视图，逐项对照，不接受受理响应：

```text
dws aitable view get --base-id <base-id> --table-id <结果表 id> --view-ids <表单视图 id>
```

- `custom.formInfo.status` 为 `1`（已发布），`shareUuid` 已生成；
- `custom.requiredFields` 中只有审批结果字段为真；
- `custom.hiddenFields` 中默认标题题字段为真（已移出表单）。

分享链接由 `shareUuid` 拼出，须实际打开页面确认题目、必填与权限与配置一致：

```text
https://docs.dingtalk.com/notable/share/form/<shareUuid>?source=link
```

通用 `aitable view update --config` 不支持表单私有配置：传 `custom` 或 `requiredFields` 会被服务端列为「不支持的 key」，返回成功但配置不变。表单必填、隐藏等只能改网页端或字段配置，且一律以独立回读为准。

## T10 运行层驱动

T07 端到端还差本地操作流水和把收集表/待办完成接进冻结引擎。本卡补这两块；**不改** `contracts/`、`workflow/`。真实 dws 联调仍归 T07，隔离测试通过不能当成 L4。字段映射拆分见下一节。

无交互重复运行（每次先回查未决流水，不盲重发）：

```text
python -m bootstrap --drive --runtime 运行目录 --lock-root 锁目录
```

绑定或 lease 缺失时 fail-closed（`CONFIG_RECONFIRM_REQUIRED` / `SECOND_INSTANCE_BLOCKED`）。第二实例抢同一主账会被拦住。`runtime/ready.json` 不能放行。

生产还须在 `binding.json` 里**显式**给出 T07 本机已确认的字段，仓库不猜测安装路径：

- `dws_cmd`：字符串数组，例如调用方注入的 `node` 与 `dws.js` 路径
- `form_container` / `todo_container`：阶段入口容器
- `fields`：台账字段 ID（`FieldMap` 全套键），必填
- `entry_fields`：阶段入口字段 ID（`EntryFieldMap`），必填

缺 `fields` 或 `entry_fields`、键不完整、或把台账整表拷进 `entry_fields`，都是 `CONFIG`。不会回落到合成标识，也不会拿台账映射去读收集表。真实字段 ID 只放本机绑定，不进仓库。

工作队列是运行目录下的 `sources.json`（Git 忽略），只存单据/来源引用，不是第二本库存账。`kind` 为 `apply` 或 `event`。`apply` 走 `admit_application` 后建立审批入口；`event` 走 `execute`，同意后系统预留，再按状态建借出/归还入口。回执落 `runtime/operations/<operation_id>.json`。

隔离测试注入假读写端口，不连真实钉钉。协作者不得索要凭据或代跑真实组织。

## T10 缺陷：台账与阶段入口字段必须拆开

两张表共用一套扁平 `FieldMap` 时，`--drive` 消费 `kind=apply` 会把入口表的 `return_id` 等键套到台账记录上，解码失败为 `EVIDENCE_REQUIRED`。重叠的八个键（`config_version`、`quantity`、`physical_ids`、`borrower`、`approver`、`manager`、`return_container`、`return_id`）在两张表上是不同 ID。适配器读借用/库存只用 `fields`；读收集表事件只用 `entry_fields`。

## T10 缺陷：假实现必须对齐钉钉回读形态

隔离传输不得把 singleSelect 的 `id` 设成与 `name` 相同，也不得回传空字符串字段。写入只发选项 name 字符串；读回必须是 `{id, name}`，适配器读 `.name`。假 dws CLI 读侧须把存盘中的 name 字符串物化成 `{id, name}`（与 MemoryTransport 一致），不得让生产解码接受裸字符串。空的归还字段按空而不是缺证。申请决定 `apply` 的 actor 是借款人。写超时后若台账和库存仍是发前快照，记 `NOT_SENT` 并允许按原操作重试；部分写入仍是 `UNKNOWN`，不重放。

## 本阶段会做什么

- 检查 Windows、Python 3.11+、文件选择能力。
- 显示绑定、授权核验记录、导入回执和闸门状态；缺绑定为未通过，不是待确认。
- 使用系统文件选择框选取文件；也可在测试中注入选择结果。
- 识别 Excel 工作簿（xlsx 容器）和 HTML 内容的 xls，展示工作表名、原始表头和原始单元格。
- 确认导入时按冻结字段名核对并独立回读。
- 支持含中文或空格的路径。取消选择、空文件、格式不符和解析失败会显示错误，不修改源文件。

## 本机文件选择核验（脱敏）

在协作者 Windows 上针对当前分支做了真实对话框核验，不连接钉钉、不读正式台账。

1. `python -m bootstrap`（与 `bootstrap/start.bat` 同一前台入口）打开系统文件选择框。
2. 第一次选择后取消，向导显示取消且未写入。
3. 再次打开对话框，选中仅含合成表头 `ColA/ColB` 的临时文件，文件名含中文与空格；预览出现原始表头和单元格，公开输出只有文件名，不含个人目录。
4. 选择错误/取消路径可见；结束后对照进程列表，无新增遗留 `python.exe`。
5. 另用 `bootstrap/start.bat` 确认双击入口也会弹出同一标题的真实对话框。

此核验证明真实文件选择框可用，不等于 T07 主控部署或 L4 钉钉验收。闸门与导入确认为注入测试，不是真实授权。

## 本阶段不会做什么

- 不调用真实 dws，不创建待办，不把预览当主账写入。
- 不自动安装软件包，不把本地预览或合成绑定当作主控验收。
- 不实现跨机器锁；不在绑定缺失时用 ready 文件或 BAT 外壳放行。

## 测试

```text
python -B scripts/repo_checks.py
python -B -m unittest discover -s tests/bootstrap -p "test_*.py" -v
```

测试用临时目录生成合成 HTML / xlsx 和绑定 JSON，运行后删除，不提交真实台账或二进制夹具。`tests/test_bootstrap_suite.py` 把 `tests/bootstrap` 下的用例交给默认发现入口。

## T07 停止点

1. 真人确认四项核验前不要把合成 `binding.json` 拷到生产运行目录。
2. 申请入口必须是已有受限收集表/记录的精确引用，不能靠可编辑标题。
3. 审批入口与申请入口是两张不同的收集表；指定人按最小授权只勾选审批人，申请人不进审批入口。
4. 导入确认不写钉钉；主账落盘仍走 T03 连接层与 T07 本机回读。
5. 锁目录被崩溃占用时停止，不要删除不明锁去抢写。
6. 字段级权限、OA、并发、跨机器未验证。
