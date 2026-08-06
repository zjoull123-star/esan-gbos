# 权限矩阵（Gate 0/1）

默认拒绝；所有动作受 `site_id`、数据分类、目的、会话有效期和审计策略
约束。矩阵定义工程权限，不是对人员是否具备法定授权的结论。

表中的 Kingdee/正式经营指标均为未来能力边界，不代表 Gate 0/1 已连接。
Gate 5 前只能使用明确标记的合成 mock；Gate 5 后也只能通过 Metrics API
或白名单只读投影访问，不能直连原始数据库。

Legend: **允许** = 仅在列出的条件下；**审批** = 可提交申请但必须由
指定 Reviewer/Approver 执行；**拒绝** = 不提供工具/接口；**紧急** =
仅 break-glass，需 ticket、时限和完整审计。

| 角色 | 业务数据范围 | Restricted 原文与证据 | AI Draft / Review | 正式状态与 Kingdee | 配置、审计与导出 |
|---|---|---|---|---|---|
| GBOS Admin | 本 site 全部 GBOS 配置与业务对象；不代替业务审批 | 默认拒绝；仅 break-glass | 可查看策略结果，不能代替被分配 Reviewer | 不签发业务批准；Kingdee 拒绝写 | 管理角色/团队；导出仍需审批和审计 |
| Integration Admin | 只读连接状态及最小映射字段 | 拒绝原文；不能读取明文秘钥 | 拒绝审核 | Gate 5 Kingdee 只读连接配置；无写工具 | 管理连接器开关、轮换引用和健康状态 |
| Privacy/Audit | 只读审计、分类、同意、保留与删除回执 | 按审计目的只读最小证据 | 只读 | 所有业务写拒绝；只读连接审计 | 可生成受控审计报告，不修改业务 |
| CEO | 全局汇总及业务对象只读 | 默认拒绝原始证据 | 可查看审核队列结果，不批准未分配事项 | Gate 5 正式指标只经 Metrics API；Kingdee 拒绝写 | 无连接配置；受控汇总导出 |
| Sales Manager | 所管理团队的客户、Deal、样品和工作项 | 仅关联业务所需最小字段 | 可创建草稿、退回团队草稿 | Deal 阶段等仍走人工命令；Kingdee 拒绝写 | 无连接配置；团队范围导出需审批 |
| Sales User | 自己或所属团队的客户、Deal、样品和工作项 | 仅关联业务所需最小字段，不得批量导出 | 可创建/修改 AI Draft，不得批准 | 不能自动改 Deal 阶段、Won/Lost、报价或外发；Kingdee 拒绝写 | 拒绝配置；按审批导出 |
| Purchase Manager | 采购协同及获授权的客户需求摘要；所管理采购团队 | 默认拒绝客户原始通信 | 可退回采购草稿或处理被分配审核 | 最终供应商选择须人工命令；Kingdee 拒绝写 | 拒绝连接配置；采购范围导出需审批 |
| Buyer | 采购协同及获授权需求摘要 | 默认拒绝客户原始通信 | 可录入候选供应商和创建草稿，不得最终批准 | 不能发布订单或写 Kingdee | 拒绝配置；按审批导出 |
| Product/R&D | Product Brief、样品迭代、寄样和反馈摘要 | 只读与样品直接关联的最小证据 | 可创建/修改样品草稿，不得越权批准 | 不能承诺价格、交期或外发；Kingdee 拒绝写 | 拒绝配置；按审批导出 |
| Reviewer | 仅被分配的 Review Case 及关联只读上下文 | 可按目的查看关联证据，禁止任意浏览 | 可批准/拒绝/标记 superseded | 可签发获授权 ApprovedCommand；不能直接修改业务主体或写 Kingdee | 不改权限/连接；可批准受控导出 |
| Finance Readonly | Gate 5 只读获治理批准的财务指标/投影及来源时间 | 拒绝原始通信 | 只读相关审核结果 | 仅 Metrics API/只读投影；Kingdee 拒绝写 | 拒绝配置和业务写；财务导出需审批 |

## 不可绕过的限制

- AI、Observer、Support 和 Kingdee connector 没有正式写入或批准工具。
- CEO/Finance 不得把原始 Observer、未确认 Fact、Decision 草稿或 AI Draft
  当作正式 KPI；指标必须携带定义版本、来源、`as_of`、freshness、coverage
  和 reconciliation 状态。
- `AI Draft` 不能写入 deal stage、won/lost、formal price、discount、
  outbound message、sales order、purchase order 等正式字段；见
  `contracts/draft-mutation.schema.json`。
- `ApprovedCommand` 必须有 actor、review case、expected revision、
  payload hash、idempotency key 和 before/after 状态；重放或旧 revision
  必须拒绝。
- Restricted 数据不得进入浏览器持久缓存、普通日志、未经审批的导出或
  未批准模型上下文；legal hold 暂停相关自动删除。
- 角色只在其 `site_id` 生效。跨 site、跨分类或批量导出按紧急流程单独
  审批，不能通过 API 参数伪造绕过。

## 待验证证据

Gate 1 需要角色矩阵的正向/负向测试、跨 site 访问拒绝、草稿禁止字段、
命令幂等/版本检查、break-glass 审计和浏览器缓存检查。文档存在不等于
这些运行时证据已经通过。
