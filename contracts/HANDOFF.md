# T02 最小MVP公共契约：交主控审查

Refs #2

## 当前结论

已实现最小公共类型、纯状态转换、身份/部署绑定校验、操作ID、读写/回查/阶段入口及排他/恢复接口，并用合成实现跑通契约测试。**这是候选v0.1.0，未冻结；不自行合并，不关闭Issue，不放行消费者。** 主控审查并合并后才视为冻结。

固定基线：`0b44ccbae67ba3822a56b293348097575e8572a7`。指定分支：`task/t02-minimal-contracts`。开工核对HEAD/分支与基线一致、工作区干净；已分页读取Issue #2全部评论，以最后主控开工卡为准。未建立额外任务或扩大开发阶段，本文件作为本卡交接入口。

## 字段、状态、接口简表

| 类别 | 本次约定 |
|---|---|
| 身份 | Identity(contact/todo + tenant + user)，IdentityBinding绑定精确task、创建参数及独立回读证据；不以姓名或指定执行人冒充实际完成者 |
| 原单/库存 | Resource完整定位；Loan固定申请人、审批人、管理人、数量/编号、归还时间、配置版本、可信申请证据；Inventory区分available/reserved/borrowed及实物ID |
| 事件 | 可信apply、明确approve/reject、系统reserve、真人confirm_issue、整笔request_return、真人confirm_return、管理人cancel |
| 业务状态 | awaiting_approval → reservation_pending → awaiting_issue_confirmation → borrowed → awaiting_return_confirmation → closed；rejected/cancelled为另两终态 |
| 写入结果 | not_sent / unknown / verified / not_applied，与业务状态分开；完整Loan和库存回读一致才推进 |
| 写入意图 | 稳定operation_id、原态与期望态、库存前后值、源事件；不同负载不得复用同ID；未知先查，部分成功不重放整笔 |
| 注入接口 | ReadPort、WritePort、StagePort、OperationStore、SingleInstance；RuntimeBinding每次底层写核验 |
| 纯函数 | accept_application、plan、verify、operation_id、check_binding、stage_operation_id、verify_stage、business_date |
| 规则边界 | VERSION=0.1.0，RULE_COVERAGE=not_covered，来源可引用；无制度规则引擎或合规结论 |

字段和每个错误/恢复规则详见[contracts/README.md](README.md)，类型定义见model.py、flow.py、ports.py。

## 实际验证

解释器：Python 3.11.15；不安装任何第三方依赖。测试命令均从仓库根运行。

| 命令 | 实际结果 |
|---|---|
| `python -B -m unittest discover -s tests/contracts -p "test_*.py" -v` | 42项通过，0失败/错误/跳过；含纯规则与合成接口集成 |
| `python -B -m unittest discover -s tests -p "test_*.py" -v` | 39项通过，0失败/错误/跳过；原bootstrap和guard回归，与上一行分别统计 |
| `python -B scripts/repo_guard.py` | 通过；最终文件纳入索引后再跑 |
| `python -B scripts/repo_guard.py --staged` | 通过；覆盖本卡新增文件，不只扫描旧tracked内容 |
| `git diff --check`、`git diff --cached --check` | 通过 |

已实际观察红→绿：首个批准契约因缺contracts失败；完整主链在缺reserve支持时失败；阶段回执测试在缺verify_stage时失败；布尔数量及缺可信申请凭据反例先失败、补齐校验后通过。不是只把已有guard通过作为业务验收。

覆盖：整笔数量/编号借还、明确拒绝、缺身份/错人/错单、仅勾待办不审批、数量不足挂起、重复预留不重复扣减、未知结果只回查、部分回读阻断取消、归还未确认不恢复数量、借出前取消释放预留、配置版本/角色变化拒绝、stage创建/回读证据、第二实例/失去lease调用约束。

L1：纯契约与失败边界。L2：注入合成writer/journal/lease的整链和未知恢复调用；独立Python进程读取临时JSON并复算相同operation_id。临时JSON由TemporaryDirectory清理。**该进程测试只证明ID稳定，不证明持久恢复存储已经实现；SyntheticLease不是OS排他实现；StagePort不是已接通钉钉。** 没跑L3/L4，没有触发真实业务，没有要求用户配合测试。

## 自查与改动边界

- Standards自查：仅contracts/、tests/contracts/、SPEC.md、design.md、tickets.md；标准库、无个人署名/路径/凭据/真实数据。备份保留为忽略的.bak，不进入提交。未改CI、检查脚本、依赖、bootstrap或他人模块。
- Spec自查：单机单账号单主账；真实身份/明确决定/确认/整笔归还/回读不可省；未知写入不推进。宽范围首版闸门已移入延期，T01历史事实未删、完整L4未写通过。
- 本轮是执行会话自查，不冒充主控独立审查或GitHub批准。无本卡内并行文件碰撞；T09共用检查维护权在规范中写清。

## 主控一次决定项

建议Asia/Shanghai每天09:00触发；晚启动只补当天一次，不补历史日或已错过的提前一天提醒；到期日不算逾期，次日起每天一次。仅borrowed提醒，归还待确认暂停。**时刻/跨日策略仍待主控确认，未实现调度或启用发送。** 取消权限和在途配置规则已按开工卡固定，不再追加全面拷问。

## 冻结后消费者如何接

- T03：实现ReadPort/WritePort/StagePort；解析T01公开字段事实，核验原单/阶段关联和实际完成者；每个写入口重验RuntimeBinding及lease；创建与修改都回查。未经实际验证的关联能力fail-closed，不伪造接口。
- T04：用RuntimeBinding、check_binding及SingleInstance接现有部署入口，只补本机绑定与排他，不扩部署外壳。
- T05：新单走accept_application；读取可信事件和库存后plan；原意图先持久保存再submit；unknown按原IDload/query/verify；阶段资源也按StageRequest保存。不得把本地快照建成第二库存账。
- T06：冻结提醒时刻后按业务日期/状态和持久防重键接提醒，不修改库存。

未实现/未验证：实际钉钉适配器、真实原单/阶段通用关联、持久存储、机器级排他、真实故障恢复；这些是后续任务按契约实现并在T07验证。T01完整权限与L4仍未通过；细分字段权限、通用审批、多机并发/迁移、网页、埋点均延期。最终生产放行由主控在T07真实完整借还及关键反向验收后决定。
