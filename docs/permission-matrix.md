# 权限矩阵（Gate 0–2）

默认拒绝；所有动作受 `site_id`、数据分类、目的、会话有效期和审计策略
约束。矩阵定义工程权限，不是对人员是否具备法定授权的结论。

表中的 Kingdee/正式经营指标均为未来能力边界，不代表 Gate 0/1 已连接。
Gate 5 前只能使用明确标记的合成 mock；Gate 5 后也只能通过 Metrics API
或白名单只读投影访问，不能直连原始数据库。

## CEO auto-elevation（当前实现）

带有 `CEO` 角色的 Frappe `User` 在 `before_validate` 中自动补齐一个封闭的
全权角色 bundle，并强制 `user_type=System User`。准确 bundle 只有以下五个
角色；已有的其他角色不会被删除，非 CEO 用户不会被提升：

```text
CEO_FULL_ACCESS_ROLES = (
    `CEO`,
    `GBOS Admin`,
    `Integration Admin`,
    `Reviewer`,
    `System Manager`,
)
```

`after_install` 与 `after_migrate` 都会运行同一幂等 backfill，覆盖已存在的
CEO 用户。该身份提升不改变 Restricted 原文策略，也不等于 real runtime、
正式 local pilot 或 production 已获批准。

Legend: **允许** = 仅在列出的条件下；**审批** = 可提交申请但必须由
指定 Reviewer/Approver 执行；**拒绝** = 不提供工具/接口；**紧急** =
仅 break-glass，需 ticket、时限和完整审计。

| 角色 | 业务数据范围 | Restricted 原文与证据 | AI Draft / Review | 正式状态与 Kingdee | 配置、审计与导出 |
|---|---|---|---|---|---|
| GBOS Admin | 本 site 全部 GBOS 配置与业务对象；不代替普通业务审批 | 默认拒绝；仅 break-glass | 仅在命令明确允许的管理员覆核路径决定，仍要求 pinned Review Case、revision、幂等与审计 | 不绕过正式命令或签发 Kingdee 写入 | 管理角色/团队；导出仍需审批和审计 |
| Integration Admin | 只读连接状态及最小映射字段 | 拒绝原文；不能读取明文秘钥 | 拒绝审核 | Gate 5 Kingdee 只读连接配置；无写工具 | 管理连接器开关、轮换引用和健康状态 |
| Privacy/Audit | 只读审计、分类、同意、保留与删除回执 | 按审计目的只读最小证据 | 只读 | 所有业务写拒绝；只读连接审计 | 可生成受控审计报告，不修改业务 |
| CEO | 通过封闭自动角色包读取本 site 全局汇总及业务对象 | 默认拒绝原始证据 | 按 `Reviewer` / `GBOS Admin` 的受控规则处理审核，不能绕过 pinned case、revision 或审计 | Gate 5 正式指标只经 Metrics API；Kingdee 拒绝写 | 通过 `Integration Admin` 管理连接状态、开关与健康；不能从 UI 读取秘钥；受控汇总导出 |
| Sales Manager | 所管理团队的客户、Deal、样品和工作项 | 仅关联业务所需最小字段 | 可创建草稿、退回团队草稿 | Deal 阶段等仍走人工命令；Kingdee 拒绝写 | 无连接配置；团队范围导出需审批 |
| Sales User | 自己或所属团队的客户、Deal、样品和工作项 | 仅关联业务所需最小字段，不得批量导出 | 可创建/修改 AI Draft，不得批准 | 不能自动改 Deal 阶段、Won/Lost、报价或外发；Kingdee 拒绝写 | 拒绝配置；按审批导出 |
| Purchase Manager | 采购协同及获授权的客户需求摘要；所管理采购团队 | 默认拒绝客户原始通信 | 可退回采购草稿或处理被分配审核 | 最终供应商选择须人工命令；Kingdee 拒绝写 | 拒绝连接配置；采购范围导出需审批 |
| Buyer | 采购协同及获授权需求摘要 | 默认拒绝客户原始通信 | 可录入候选供应商和创建草稿，不得最终批准 | 不能发布订单或写 Kingdee | 拒绝配置；按审批导出 |
| Product/R&D | Product Brief、样品迭代、寄样和反馈摘要 | 只读与样品直接关联的最小证据 | 可创建/修改样品草稿，不得越权批准 | 不能承诺价格、交期或外发；Kingdee 拒绝写 | 拒绝配置；按审批导出 |
| Reviewer | 仅被分配的 Review Case 及关联只读上下文 | 可按目的查看关联证据，禁止任意浏览 | 可批准/拒绝/标记 superseded | 可签发获授权 ApprovedCommand；不能直接修改业务主体或写 Kingdee | 不改权限/连接；可批准受控导出 |
| Finance Readonly | Gate 5 只读获治理批准的财务指标/投影及来源时间 | 拒绝原始通信 | 只读相关审核结果 | 仅 Metrics API/只读投影；Kingdee 拒绝写 | 拒绝配置和业务写；财务导出需审批 |

## 服务身份

服务身份不是业务用户，也不能继承 System Manager。所有服务调用均须
**每请求**验证 issuer、`audience`、`site_id`、`purpose`、最小 `scope`、
数据分类、资源和过期时间；服务不能把上游 token 直接传给下游，也不能读取
不属于自身连接的明文凭据。下表的“最早 Gate”保留最初能力分期；当前分支已实现
本地 DB/HTTP 服务鉴权，以及两个 desk-less Frappe 身份 `Observer Identity Resolver`
和 `Agent TrustedMaterializer`。这些实现与合成/隔离测试不等于正式 local pilot 已启动：
checked-in manifest、真实渠道、真实模型、Kingdee、cloud 与 production 仍保持关闭。

| 服务身份 | 最早 Gate | 允许范围 | 永久或当前拒绝 |
|---|---:|---|---|
| `observer-ingest` | 3 | 当前 site 的获批 connector 事件、checkpoint、隔离区及 EvidenceRef | 不确认事实、不创建 Decision/Action/DraftMutation；无业务数据库和 Kingdee 权限 |
| `context-service` | 3 | 当前 site 的证据、Fact Proposal、实体解析提案、双时间与 provenance；Gate 4 才可保存经决策确认的事实 | 不直连 Frappe/MariaDB；不执行 ApprovedCommand、正式指标或外部副作用 |
| `agent-runtime` | 4 | 在预算、purpose、分类与工具白名单内读取最小 Context，并提出内部 Action/AI Draft/Work Item | 不直连 Frappe/MariaDB；不外发、不报价、不改 Won/Lost、不建订单；无 Kingdee 权限 |
| `gbos-bff-service` | 4 | 在委托用户、Review Case、revision、幂等键和 Action Guard 均有效时执行白名单 GBOS 内部命令 | 不浏览 Restricted 原文；不代理任意 DocType/SQL；无 Kingdee 写工具 |
| `metrics-service` | 5 | 读取已治理 workflow/read projection 并返回带 definition、lineage、freshness、coverage、reconciliation 的指标 | 不读取未确认 Fact/AI Draft，不执行任意 SQL，不把模型估算作为正式 KPI |
| `kingdee-adapter` | 5 | 使用独立受限身份和 `kingdee-read` scope 查询 metadata 与白名单只读投影 | 无 Kingdee 写工具；不接受 raw Form/字段/SQL/URL，不共享用户或模型 token |

当前 Frappe 服务身份使用固定用户和单一服务角色，均为 `desk_access=0`，凭据只从
repository-external `0600` 文件装载：`gbos-identity-resolver@localhost.invalid`
仅持有 `Observer Identity Resolver`，可调用精确的身份解析方法但没有通用 DocPerm；
`gbos-materializer@localhost.invalid` 仅持有 `Agent TrustedMaterializer`，只能执行
封闭的 AI Draft / Review 物化命令。两者都不能继承 CEO、GBOS Admin 或 System Manager。

任何服务身份新增 scope、跨 site 访问或外部出站目标，均需要重新更新本矩阵、
威胁模型、负向测试和变更审批。服务身份不能审批自己的提案，也不能将
`controlled_by_disabled_capability` 解释为运行时安全已经通过。

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

## 证据状态

Gate 1 已通过角色正向/负向、团队隔离、草稿禁止字段、命令幂等/revision、
浏览器缓存和业务闭环测试；其历史证据受
[`docs/evidence/SHA256SUMS`](evidence/SHA256SUMS) 保护。break-glass 的真实
组织演练仍未执行。

Gate 2 历史证据只验证服务身份、scope、purpose 和默认拒绝的设计契约；当前分支
随后增加了本地服务组合、Frappe 服务身份、跨 site/RLS、重放和审计脱敏测试。
正式 local pilot 仍为 No-Go，真实渠道/provider 与生产环境的 audience/resource、
撤销传播、数据分类、出站控制和组织演练证据仍须单独补齐；代码和合成测试不能替代
这些现场证据。
