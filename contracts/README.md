# 最小借还公共契约 v0.1.0（候选，尚未冻结）

Refs #2。主控审查并合并后才冻结，不自动放行消费者。

适用：一台使用者电脑、一个运行账号、一个活动实例、一套钉钉主账、一笔整单借还。Python 3.11+标准库，无平台调用、数据库、锁服务、网页或规则引擎。`tests/contracts/synthetic.py`是**合成测试实现**，不能用于真实业务。

## 1. 来源与可信边界

- 范围：[Issue #2使用者确认](https://github.com/lqhoyh121-afk/tool-management-loop/issues/2#issuecomment-5660798328)、[最新主控卡](https://github.com/lqhoyh121-afk/tool-management-loop/issues/2#issuecomment-5660952693)、SPEC.md。
- 能力事实：[T01-HANDOFF](../docs/evidence/T01-HANDOFF.md)、[T01-FOLLOWUP](../docs/evidence/T01-FOLLOWUP.md)。已证明的只是所测收集身份、明确决定、创建/回读到完成身份及正常读写事实；不是平台事务、通用审批关联、完整权限或完整L4通过。
- `VERSION`、`RULE_SOURCES`、`RULE_COVERAGE="not_covered"`在`contracts/__init__.py`。版本是公共契约版本，**不是安全制度版本或合规结论**。首版不求值制度条款。
- Python dataclass是适配器的**内部规范化值**，不是员工请求JSON接口。构造对象或传入`verified=True`不证明权限。T03必须从源系统creator、活动及配置回读证据生成Event；员工永远不能直接指定角色、verified或状态。T05入口必须调用ReadPort，不从聊天文字或手填姓名造可信事件。
- 普通人主账全拒绝、受限表单入口、管理账号维护主账。程序只消费真人决定与确认，不代勾。配置核验、进程排他和持久流水是后续实现义务，不是这些Protocol已提供的能力。

## 2. 最少字段与序列化约定

Python类型定义为唯一字段清单（`model.py`、`ports.py`）。存储/传输映射由T03/T05实现：枚举用value，datetime用含偏移ISO时间，tuple用JSON数组；数量规范化为严格正整数（库存可为0，拒绝bool、小数、空值），不得猜补。实物编号按原字符串保存、确定排序后传tuple，不用UUID冒充实物编号。现代业务日期（2000年及以后）按Asia/Shanghai；`business_date`用现代UTC+08转换，避免Windows额外tzdata依赖，不支持历史时区考证。

| 类型 | 必需字段/意义 | 来源及约束 |
|---|---|---|
| Identity | namespace、tenant_id、user_id | contact为通讯录；todo为待办内部ID。同字符串不同命名空间也不是同人；不得用姓名、modifierId或创建人替代实际完成者 |
| Resource | kind、tenant_id、container_id、resource_id | kind=record/form/todo；container_id是适配器保存的完整父资源定位（多维表须含Base和表的无歧义定位），不是名称；全引用相等才同目标 |
| IdentityBinding | contact、internal、task、creation_evidence、readback_evidence | task限定映射：保存create executors精确通讯录参数、创建响应taskId、初始唯一内部执行者及完成活动回读。不能跨task缓存套用 |
| LedgerScope | tenant_id、container_key | 整套主账的稳定定位：租户+无歧义父容器（多表主账用确定拼接键）。RuntimeBinding.ledger与SingleInstance排他都按此scope；单条物资record Resource只是借用级引用，绝不作为排他键 |
| Loan | ref、item、borrower、approver、manager、quantity、tracked、physical_ids、due_at、config_version | ref为原借用申请记录，item为一种工具主账记录；角色在创建配置中固定；需追踪时编号数=数量且不重复；缺编号停止待补充 |
| Loan处理字段 | state、return_ref、consumed_events、application_evidence | 归还记录独立引用原借用；只整笔归还。consumed_events存已接受源事件键，不存扫描时间；application_evidence保存可信申请证据定位，缺失时plan拒绝推进 |
| Inventory | ref、available/reserved/borrowed、三组对应IDs、revision | 预留从available移到reserved；借出reserved移到borrowed；归还borrowed移回available；取消只释放reserved；全组ID不能重叠 |
| Event | action、event_id、loan_ref、source、actor、occurred_at、config_version、evidence_kind、verified、evidence_ref | 源系统可信事件；event_id须包含源资源限定，使一张借用单的不同来源间也不碰撞；必须持久稳定。evidence_ref是受保护的证据定位，不是日志里的PII；原单/阶段关联必须由适配器核验 |
| Event附加字段 | binding、return_ref、quantity、physical_ids | 确认事件必有task身份映射；申请/归还需实际数量及编号；归还还需独立原记录引用 |
| RuntimeBinding | account、ledger(LedgerScope)、config_version、approver、manager、evidence_ref及4个显式核验项 | 4项为普通人主账拒绝、受限入口、管理账号读写、本人确认；缺项默认False，底层每次调用check_binding；ledger按主账scope判断，同主账不同物资记录均接受，跨主账/跨表拒绝WRONG_LOAN |
| WriteIntent | operation_id、before/after、stock_before/stock_after、event | 原状态、预期状态及证据；只描述一动作，生成不等于写入 |
| Receipt | operation_id、outcome、readback_evidence、loan、inventory | 完整独立回读才verified；不得只凭HTTP/CLI成功或服务端受理报成功 |
| StageRequest/StageReceipt | operation_id、loan/action/actor；结果source/binding及创建/回读证据 | 为该原单/阶段建立受限入口或独立待办；创建同样要操作防重和回查 |

归还、审批、确认的资源关联不能只存可编辑标题。T03须保存`原单完整引用 + action + config_version + 指定人 + 精确阶段资源引用 + 创建证据 + 回读证据`。`ReadPort.read_event`须与这份绑定比对，完成活动还须是对应task的新活动。无法证明唯一关联或读到冲突决定就报EVIDENCE_REQUIRED，不能“挑最新一条当正确”。借出和归还使用不同task；同一task的task.self.done/task.done表示同一次确认，适配器归一为同一事件键，不当两个业务动作。

## 3. 状态、事件与库存

| 前态 | Action | 放行事实 | 回读后态 / 库存变化 |
|---|---|---|---|
| 新申请 | apply | 可信提交人=borrower，数量/编号一致，预计归还晚于提交 | accept_application规范化为awaiting_approval；不发放 |
| awaiting_approval | approve | 指定审批人在受限表单明确同意 | reservation_pending；库存不动 |
| awaiting_approval | reject | 指定审批人明确拒绝 | rejected；库存不动 |
| reservation_pending | reserve | 单写入者读到足量可用库存/编号 | awaiting_issue_confirmation；available→reserved |
| awaiting_issue_confirmation | confirm_issue | 指定管理人真实完成关联借出待办 | borrowed；reserved→borrowed |
| borrowed | request_return | 原借用人提交关联原单的整笔归还 | awaiting_return_confirmation；库存不动 |
| awaiting_return_confirmation | confirm_return | 指定管理人完成独立归还确认待办 | closed；borrowed→available |
| 尚未借出的三种状态 | cancel | 指定管理人在受限入口明确取消 | cancelled；有预留则reserved→available，无预留不改库存 |

其余转换拒绝，包括拒绝后借出、借出后取消、没有归还申请直接确认归还。不自动超时释放。库存不足或条件冲突报RESERVATION_CONFLICT，保持reservation_pending并显式挂起；不生成写意图、不创建借出确认。读到条件变化后可重新规划尚未发送的动作，不能重写已经prepare的原意图。

### 3.1 阶段入口前态约束（含StageRequest构造校验）

| Action | 允许的业务前态 | 说明 |
|---|---|---|
| approve | awaiting_approval | 表单承载同意/拒绝 |
| confirm_issue | awaiting_issue_confirmation | 预留成功后才允许创建借出确认；未审批/预留失败/数量不足一律不建 |
| request_return | borrowed | 未借出不建归还申请入口 |
| confirm_return | awaiting_return_confirmation | 无归还申请不建归还确认待办 |
| cancel | awaiting_approval、reservation_pending、awaiting_issue_confirmation | 终态（rejected/cancelled/closed/borrowed）一律拒绝INVALID_STATE |

StageRequest在构造时即校验前态与角色；阶段资源已发出不受plan状态检查追溯约束，所以入口必须前置拦截。apply阶段由T04绑定原申请入口，不经StageRequest。

`plan`只返回预期。所有写入（包括拒绝/取消/审批状态）都需回读才发布新状态。`verify`核operation_id、完整Loan、库存数量/编号/目标，库存revision为新读取得到的条件令牌，不能要求新revision等于写前revision。归还回写未定保持旧业务状态 + unknown，T06此时也暂停催借用人。

## 4. 操作ID、未知结果与恢复

`operation_id(loan,event)`以原单完整引用、action、源资源和事件键生成确定UUID5；相同输入跨扫描/重启/进程相同。ID不含当前时刻、随机重试数或人名。approve/reject属于互斥状态转换，不靠不同ID允许覆盖已有决定。

1. 单实例获取机器排他权；读取主账、配置及源事件，生成纯计划。
2. `OperationStore.prepare`在外发前持久保存**原意图全量**，同ID不同负载拒绝；同单/同工具存在未决操作时拒绝后续动作。
3. 发送前保存unknown标记，再调用底层submit。即使进程在网络调用处崩溃，恢复时也是回查，不重发。
4. submit必须重验lease、RuntimeBinding和fresh前态/revision；变了就无写入拒绝。单机排他不是平台事务，管理人直接改库存前须停止自动处理并在恢复前重验。
5. 未读回所有目标：unknown。`load`原意图→`query`精确所有目标→`verify`，结果一致才保存verified。本地流水可留前后快照用于比较，但钉钉仍是唯一业务主账。
6. 部分写入/查询不完整/无权限/空结果/索引延迟均保持unknown，阻断同单同物资写入。后续T03/T05须保存子步骤回执并只对已查明未完成部分恢复；不能重放整个意图。无法确证则停止交主控。
7. not_sent只表示已知从未发出；not_applied须独立查明所有目标未写且无在途受理，不可仅凭“未找到”。这两种情况在重新核验权限与前置条件后才允许原意图重试；条件变化需要关闭旧未写操作并记录新源事件关联，不能换ID绕开unknown。verified重放返回原回执，不再增减库存。

| 错误码 | 处理，不默认自动重试 |
|---|---|
| INVALID_INPUT / QUANTITY_MISMATCH / PHYSICAL_IDS_REQUIRED | 修正真实输入或待补充，不自动造值 |
| IDENTITY_REQUIRED / WRONG_PERSON / WRONG_LOAN / EVIDENCE_REQUIRED | 拒绝放行；补可信来源或修复关联，不改角色猜测 |
| CONFIG_RECONFIRM_REQUIRED | 停止并显式重新确认路由；在途版本不得静默覆盖 |
| INVALID_STATE / DUPLICATE_EVENT | 不写；重复需load原回执，冲突决定交核查 |
| RESERVATION_CONFLICT | 保持待预留或原态，显示冲突；先重读，不伪造成功 |
| WRITE_UNKNOWN_QUERY_FIRST / READBACK_MISMATCH | 禁止submit；query原意图所有目标；未厘清不取消、不释放 |
| OPERATION_ID_CONFLICT | 同ID不允许变负载；停止人工核查 |
| SECOND_INSTANCE_BLOCKED | 第二实例/失去lease停止，不用其他目录、账号绕开 |

## 5. 注入接口和消费者

具体签名及副作用见`ports.py`，不承诺现成钉钉端点。所有外部调用必须有有限超时；查询可有界重试（建议单次30秒、至多3次后挂起），写超时一律unknown，不能自动套通用重试。权限失效立刻停止。协议/解析失败不转换成空成功。

| 消费者 | 调用边界 | 首版交接 |
|---|---|---|
| T03 | 实现ReadPort、WritePort、StagePort | 精确记录/库存/可信事件读取；受限表单或独立task建立；提交/回查；每次写检查check_binding与lease；原始number字符串等显式解析，处理业务error而不只看success |
| T04 | RuntimeBinding、check_binding、SingleInstance | 本机账号/主账/受限入口/角色显式绑定；机器范围独占，第二实例禁止；保留已有可见BAT，不扩外壳 |
| T05 | accept_application、plan、verify、OperationStore | 新申请先核验；注入T03/T04接口；持久原意图和阶段绑定、发前标记、恢复与串行处理；不另造字段/状态/客户端 |
| T06 | Loan.state/due_at、business_date、持久操作防重语义 | 仅borrowed提醒；归还待确认及任何未知归还写入均暂停；调度实现不属于本契约 |

StageRequest的operation_id调用`stage_operation_id(loan,action)`，由原单完整引用、`create_stage`及阶段确定生成并持久保存；同原单同阶段只建一次。`verify_stage`校验创建和回读证据及TODO映射，受理不明保持unknown；首版不凭缺失查询将创建判为not_applied。approve入口同时承载同意/拒绝。ISSUE/RETURN为两个独立task，StageReceipt需要task限定IdentityBinding；表单无需todo绑定。confirmed来源须与已持久的阶段绑定一致。建立阶段入口不会推进业务状态。申请原入口由T04绑定，T03读取已有申请；不创建第二份借用申请来“导入”。

### 5.1 排他与主账范围

`SingleInstance.acquire(scope, account)`以`lease_key(scope)=tenant_id::container_key`为唯一排他键：不含物资recordId、运行账号或工作目录。同主账另一物资记录、另一账号、另一目录的第二个实例同样被拒；不同主账scope互不阻塞，各持独立lease。`OperationStore`同时接受WriteIntent和StageRequest：按原单引用（及写意图的物资引用）拦截未决冲突，混合阶段/业务意图同样受检，不能只支持一种。SyntheticJournal是合成参考实现。

## 6. 提醒与配置：主控一次确认项

**建议，尚未启用：**Asia/Shanghai每天09:00，启动晚于09:00则只补当天一次；不补历史日、不补已错过的提前一天提醒。到期日不算逾期，次日起每天一次。提前提醒键=`原单+before_due+预计归还业务日期`；逾期键=`原单+overdue+当天业务日期`。只在borrowed发送，待归还确认立即暂停，不催管理人。发送unknown按原键回查，不能换日期键绕过同单尚未厘清的发送。

主控可一次接受或调整上述时刻/跨日策略；确认前T06不能启用发送。本次不写调度引擎，不增加用户配合测试。在途单保留创建配置版本/角色；普通新配置只影响新单。身份路由变化要显式重新确认并有审计关联，当前未实现时停住，不自动迁移旧单。

## 7. 验证和边界

运行于仓库根目录：

```text
python -B -m unittest discover -s tests/contracts -p "test_*.py" -v
python -B scripts/repo_checks.py
python -B scripts/repo_guard.py
python -B scripts/repo_guard.py --staged
git diff --check
```

旧unittest发现入口不自动纳入contracts子目录，必须显式运行第一条；未修改T09发现入口。测试输入全部为synthetic，不含真实人员/主账/物资。具体非零结果见HANDOFF.md。

L1证明纯转换与拒绝边界；L2仅证明合成适配器/流水/排他之间的调用约束及独立进程+临时JSON的操作ID稳定。**没有实现或验证真实持久恢复、OS排他、平台事务、真人审批体验、L3或L4。**StagePort是接口定义，真实创建/回查留T03。T07真实完整借还及关键反向验收通过前不能生产放行。细分字段权限、通用审批框架、多机并发/迁移、网页、埋点均延期。
