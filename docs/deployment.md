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
3. 导入确认不写钉钉；主账落盘仍走 T03 连接层与 T07 本机回读。
4. 锁目录被崩溃占用时停止，不要删除不明锁去抢写。
5. 字段级权限、OA、并发、跨机器未验证。

## T10 运行层驱动

T07 端到端还差本地操作流水和把收集表/待办完成接进冻结引擎。本卡补这两块；**不改** `contracts/`、`workflow/`、`integrations/dingtalk/`。真实 dws 联调仍归 T07，隔离测试通过不能当成 L4。

无交互重复运行（每次先回查未决流水，不盲重发）：

```text
python -m bootstrap --drive --runtime 运行目录 --lock-root 锁目录
```

绑定或 lease 缺失时 fail-closed（`CONFIG_RECONFIRM_REQUIRED` / `SECOND_INSTANCE_BLOCKED`）。第二实例抢同一主账会被拦住。`runtime/ready.json` 不能放行。

生产还须在 `binding.json` 里**显式**给出 T07 本机已确认的字段，仓库不猜测安装路径：

- `dws_cmd`：字符串数组，例如调用方注入的 `node` 与 `dws.js` 路径
- `form_container` / `todo_container`：阶段入口容器
- 可选 `fields`：字段 ID 映射；省略则只用合成标识，不能对真实表

工作队列是运行目录下的 `sources.json`（Git 忽略），只存单据/来源引用，不是第二本库存账。`kind` 为 `apply` 或 `event`。`apply` 走 `admit_application` 后建立审批入口；`event` 走 `execute`，同意后系统预留，再按状态建借出/归还入口。回执落 `runtime/operations/<operation_id>.json`。

隔离测试注入假读写端口，不连真实钉钉。协作者不得索要凭据或代跑真实组织。
