# Gate 2–5 测试策略与证据所有权

状态：Gate 2 设计冻结候选。本文把 contract/schema/mock 证据与后续
runtime、账号、渠道、Kingdee 和生产证据分开；未运行的检查必须记录
`not_run`，不能由文档或字符串存在推断通过。

## 原则

1. 严格 TDD：每个行为先运行可解释的 RED，再写最小实现并运行 GREEN。
2. 正向与负向测试同等重要；权限、site、字段、状态、预算和 writer-shaped
   输入默认失败关闭。
3. schema 只验证单记录形状；timeline 单调性、action stage 顺序、聚合唯一性
   等跨记录约束由 Python 测试验证。
4. fixture 与 mock 必须 deterministic、synthetic、zero-network、
   zero-credential；不得把 mock 结果描述成真实 connector 或业务验证。
5. 每个结论绑定命令、退出码、精简结果、文件 checksum、限制和 evidence
   owner。大日志进入有界保留的 CI artifact，不写入 Git。

## Gate 2 自动化矩阵

| 测试面 | 测试 owner | 证据 owner | 主要测试/命令 | 退出条件 |
|---|---|---|---|---|
| JSON Schema 与 example | Contract owner | Governance/evidence owner | `pytest tests/contracts -q` | 所有 schema 本地注册；引用不访问网络；正反例通过 |
| OpenAPI 与语义 manifest | Contract owner | Governance/evidence owner | Gate 2 OpenAPI/semantic tests | 只有 typed/versioned endpoint；无任意 SQL/DocType/Form/writer |
| Kingdee synthetic adapter | ERP integration owner | Governance/evidence owner | `pytest tests/fixtures/test_gate2_kingdee.py -q` | 七对象只读 surface；未知字段/过滤/writer 拒绝；零网络/凭据 |
| 治理与容量 | Governance/evidence owner | Governance/evidence owner | `pytest tests/governance/test_gate2_design.py -q` | 四真相、边界、容量假设、失败关闭和 scope Gate 明确 |
| 证据包 | Governance/evidence owner | Governance/evidence owner | `pytest tests/acceptance/test_gate2_evidence.py -q` | 历史 manifest 未变；Gate 2 本地 checksum 与结构化风险处置通过 |

Gate 2 的 Go/No-Go 由主代理整合完整测试、静态检查、安全检查、checksum 和
人审后作出。本文件 owner 不单独批准 release、外部连接或生产变更。

## Gate 3：观察与证据 MVP

| 项目 | 测试 owner | 证据 owner | 必需的负向测试与退出条件 |
|---|---|---|---|
| 首个 connector 与 Observer | Observer team | Observer + Privacy | 签名/鉴权失败、重放、乱序、checkpoint、撤回、rate/size、dead-letter |
| Evidence/object lifecycle | Observer + Storage | Privacy/Audit | hash/offset 可复核，恶意文件/压缩炸弹隔离，保留/删除/legal hold 回执 |
| site 与 consent | Security + Observer | Privacy/Audit | 跨 site/团队、未知 purpose、撤回后处理、未获批个人渠道均失败关闭 |
| 提取与实体解析 | AI governance + Context | AI governance | 工具-free、提示注入、错误实体、低置信、冲突；只产 proposal/Review Case |

Gate 3 退出条件是获批测试账号与 fixture 都可重放、重复事件不重复建档、
证据定位可复核、租户隔离和提示注入负向测试通过。它不授权 Agent Runtime、
真实金蝶或正式 KPI。

## Gate 4：Agent Runtime、Context/Decision 与人工审核

| 项目 | 测试 owner | 证据 owner | 必需的负向测试与退出条件 |
|---|---|---|---|
| Agent Runtime | AI platform | AI platform + Security | 并发 claim、lease 丢失/回收、崩溃恢复、幂等、max attempts、dead-letter |
| budget 与 sandbox | AI platform + Security | Security | token/cost/time 预算、rate limit、越权 tool、错误 site/purpose 默认拒绝 |
| Fact/Conflict/Decision | Context team | Context + Privacy/Audit | 无证据事实、冲突静默覆盖、错误 valid/recorded time、缺人审均拒绝 |
| Action Guard | Workflow + Security | Privacy/Audit | proposed→approval→execution→verification 顺序；商业承诺/外发/写操作需人审 |

Gate 4 退出条件是 Agent 重复领取不重复执行、lease 可恢复、预算与权限失败
关闭、事实可追溯、人工 override 完整、Action Guard 正负矩阵通过。真实
Kingdee、正式 Metrics 和 production 仍为 No-Go。

## Gate 5：Metrics、Kingdee 只读与预生产

| 项目 | 测试 owner | 证据 owner | 必需的负向测试与退出条件 |
|---|---|---|---|
| Metrics API/read model | Analytics | Analytics + Finance owner | 定义、unit/window/exclusion、lineage、freshness、coverage、reconciliation；任一失败返回 unavailable |
| Kingdee metadata/read | ERP integration | ERP integration + Security | 启动、认证、metadata、业务查询四步分证；每请求 auth、字段/行数/过滤预算 |
| MCP/出站控制 | Security + ERP integration | Security | audience/resource 错配、token 重放/混淆、SSRF、redirect、host/field/tool 越权 |
| Crosswalk 与对账 | ERP integration + Analytics | Finance owner | 源单 drill-through、重复/缺失/延迟、断点增量、对账差异失败关闭 |
| 新加坡预生产 | Platform + Privacy | Privacy/Legal + Release owner | 数据流/DPA/驻留、恢复、性能、UAT、回滚与故障演练 |

Gate 5 退出条件要求真实只读账号由独立检查验证，tool discovery 没有 writer，
正式 KPI 缺少任何治理字段即不可用，预生产安全/隐私/性能/恢复/UAT 证据
通过。金蝶写调用永久为 0。

## 安全处置与人审

每个风险记录必须包含 risk ID、severity、owner、status、具体 `test_refs`、
具体 `evidence_refs` 和 `human_review`。Critical/High 不能只凭控制名称或
关键词出现关闭；必须有可重放的负向测试与证据，或者因能力保持 disabled
而明确记录为受边界控制。运行时未启动时，runtime security 应标
`not_applicable`，而不是 `pass`。

## 证据包工作流

1. 保存 RED 与 GREEN 的完整命令、退出码和测试计数。
2. 分别运行 contract、Kingdee fixture、governance、acceptance 以及全仓
   regression；任何未运行项为 `not_run`。
3. 运行 ruff、format、mypy、secret scan 和 `git diff --check`，按结果记录，
   不用一个检查替代另一个。
4. 单独验证历史 `docs/evidence/SHA256SUMS`；其 manifest SHA-256 固定为
   `a6a86c5dcb39d5d57b27e3cf7b444f71700bd74db362a74af6b2816186982cea`。
5. `docs/evidence/gate2/SHA256SUMS` 只覆盖 Gate 2 四个紧凑证据文件，不包含
   `../gate0*`、`../gate1*` 或自身。
6. 主代理以最终 implementation commit、全仓测试计数和 checksum 校准证据；
   校准前 Gate 2 保持 conditional，runtime/production 保持 No-Go。
