# ADR-0009: 四个真相层、Agent/Context 架构与 Gate 顺序

- Status: Accepted
- Date: 2026-08-06
- Scope: ESAN GBOS Gate 2–6
- Supersedes: ADR-0003 中“Gate 3 live read canary”的时间安排
- Preserves: ADR-0003 的金蝶只读且无写工具边界

## Context

Gate 0/1 已完成 Frappe CRM、GBOS 业务闭环、权限、BFF、PWA 和 fixture
验证。后续讨论明确了两项必要调整：

1. 业务工作流、沟通上下文、正式 ERP 交易和官方经营指标具有不同的权威
   来源，不能继续用一个笼统的“Business Intelligence Layer”表达。
2. 金蝶真实连接如果早于证据、事实、决策、指标和 Agent 安全边界，会把
   集成可用误当成数据可信，因此真实 MCP/Adapter 必须延后。

## Decision

1. 采用四个真相层：
   - 金蝶是 Transaction Truth。
   - Frappe CRM + `esan_gbos` 是 Workflow Truth。
   - Observer + Context/Decision Service 是 Context Truth。
   - Metrics API 是 Analytical Truth。
2. 正式业务链固定为：
   `Observation → Evidence → Fact Proposal → Verified Fact → Decision →
   Action Proposal → Approval → Execution → Verification`。
3. Agent 使用独立、持久的 Runtime；任务包含 lease、priority、budget、
   due/recheck、retry、dead-letter、evidence 和 timeline。Agent 不直连
   MariaDB、PostgreSQL 或金蝶，只调用白名单服务契约。
4. 所有 Agent 动作经统一 Action Guard。模型只能自动读取和提出内部 draft；
   商业承诺、正式状态和外部副作用必须按策略由人确认。
5. Context Graph 首先用 PostgreSQL provenance/temporal 表与图投影实现。
   未经真实用例和性能证据，不引入专用图数据库。
6. CEO 正式 KPI 只从 Metrics API 获取；LLM 和 Context Graph 不即时生成
   官方经营数字。
7. Gate 顺序调整为：
   - Gate 2：Agent、Context、Metrics、Kingdee 契约/字段映射/mock 设计，
     零真实外部连接。
   - Gate 3：观察、证据、事实提案和最小 Context Service。
   - Gate 4：持久 Agent Runtime、Context/Decision、Action Guard 和人工审核。
   - Gate 5：Metrics API、CEO 驾驶舱、金蝶只读 MCP 实连和预生产试点。
   - Gate 6：生产发布。

## Consequences

- Gate 0/1 证据文件、校验和和历史结论保持不变；旧文档中的后续 Gate
  预测由本 ADR 覆盖。
- Gate 2 可研究字段、契约和 mock，但金蝶网络、认证、metadata 和业务查询
  必须保持为零。
- Gate 3/4 不得用 mock 金蝶数据标记正式指标，也不得提前增加
  `kingdee-read` 运行权限。
- Gate 5 启用金蝶只读能力时仍须满足 ADR-0003；本 ADR 不授权任何写操作。
- 受治理指标必须携带定义版本、来源、时间、新鲜度、覆盖率、对账状态和
  不可用原因。

## Verification

- Gate 2–4 的网络测试证明没有真实金蝶请求或凭据。
- MCP 工具发现和负向测试证明不存在 Kingdee writer、任意 SQL、任意
  DocType/Form 或直接数据库写工具。
- Agent 并发领取、lease 回收、幂等、预算、权限和 dead-letter 有运行证据。
- 事实、决策和动作均可从稳定 ID 回溯到原始 EvidenceRef。
- Gate 5 只读 canary 必须分别通过启动、认证、metadata 和白名单业务查询，
  任一步失败都不得报告为“已接通”。
