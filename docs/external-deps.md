# 外部依赖与 Gate 证据

状态仅描述当前仓库的锁定/开关，不代表供应商合规、服务可用或生产已
接通。`blocking gate` 是放行前必须满足的条件；`evidence` 链接工程
记录或明确标注待补证据。`production disabled` by default；生产采集、真实模型和
真实金蝶访问默认关闭。当前身份解析 roadmap 的来源基线是 `8c40731`；最终本地镜像和
运行验证绑定 `098d728cc52e27b6f58b051dfeb925efdfc680c4`。当前证据与历史快照的边界见
[HANDOFF](HANDOFF.md)。

| 依赖/能力 | Owner | Status | Blocking gate | Evidence |
|---|---|---|---|---|
| Frappe Framework `v16.30.0` | Platform | Frozen; fresh three/four-App installs and migrations verified locally | Gate 0 complete; upgrade requires a new compatibility gate | [compatibility matrix](compat/compatibility-matrix.md), [versions](compat/versions.json), [Gate 0](evidence/gate0-summary.md) |
| ERPNext `v16.31.0` | Platform | Frozen; installed and transaction guard verified locally | Gate 0 complete; ERPNext transactional UI remains disabled in V1 | [compatibility matrix](compat/compatibility-matrix.md), [Gate 1](evidence/gate1-summary.md) |
| Frappe CRM `v1.81.0` | Platform | Frozen; metadata contract and runtime verified locally | License review remains blocking before external service | [compatibility matrix](compat/compatibility-matrix.md), [license baseline](compat/license-baseline.md) |
| `frappe_docker` `v3.2.2` | Platform | Frozen; exact source build and healthy core stack verified | Rebuild only from locked source and clean commit | [compatibility matrix](compat/compatibility-matrix.md), [image build](compat/image-build.md) |
| MariaDB `11.8` image | Platform | Digest locked; Gate 6 local backup/restore parity verified | Production backup target, PITR and regional DR evidence remain external production gates | [versions](compat/versions.json), [Gate 6](evidence/gate6/gate6-summary.md) |
| PostgreSQL 17 + pgvector `0.8.2` image | Data platform | Digest locked; Gate 3–5 schemas, forced RLS and integration tests plus Gate 6 local dump/restore parity verified | Production HA, backup/PITR, retention and regional disaster recovery remain external production gates | [versions](compat/versions.json), [Gate 6](evidence/gate6/runtime-validation.json) |
| Redis `6.2-alpine` image | Platform | Digest locked; cache, queue, worker and scheduler health verified locally | Production HA/monitoring remains Gate 5/6 | [versions](compat/versions.json), [Gate 1](evidence/gate1-summary.md) |
| Python `3.14.2` / Node `24.13.0` | Platform | Exact runtime verified; final Node scope reduced to realtime dependencies | Upgrade requires compatibility, scan and regression evidence | [compatibility matrix](compat/compatibility-matrix.md), [Gate 1](evidence/gate1-summary.md) |
| Gate 0/1 High/Critical exceptions | Security | 历史 Gate 0/1 仍保留 57 条到期记录 / 103 个 exact PURL；当前 `098d728` local-runtime 与 Frappe 镜像经 Debian 12.15 安全更新后，固定 Trivy 0.73.0 复扫为 0 个未豁免 High/Critical、0 secrets、0 misconfigurations | 历史例外不自动转成生产批准；任何新镜像仍须重新扫描和独立签字 | [security exceptions](governance/security-exceptions-gate01.md), [current runtime evidence](evidence/identity-resolution-runtime/identity-resolution-runtime-summary.md) |
| Kingdee K3Cloud/MCP read connector | ERP integration | Gate 5 exact read-only adapter and synthetic transport implemented; live transport disabled; no write tool exists | Restricted `AI_ReadOnly` account, approved destination, consent/data review and startup → auth → metadata → business-query canary evidence remain `blocked_external_input` | [ADR-0003](adr/ADR-0003-kingdee-v1-read-only.md), [Gate 5](evidence/gate5/gate5-summary.md) |
| Channel connectors (email, WeCom, WhatsApp, phone, meeting, file, manual import) | Observer | Gate 3 normalization/replay/evidence、身份解析与持久 work queue 已用 synthetic 输入验证；production ingestion disabled，真实连接数为 0 | Provider authorization、激活时间、目标 team/account user、凭据、retention、rate/size、malware 和 tenant-isolation evidence 按真实 connector 单独放行 | [Observer boundary](../services/observer/README.md), [current runtime evidence](evidence/identity-resolution-runtime/identity-resolution-runtime-summary.md) |
| DeepSeek model gateway | AI governance | Gateway implementation and configuration are present for `https://api.deepseek.com` / `deepseek-v4-flash`; deterministic Gate 4 agent and review path are implemented, but no real call or response model identity has been observed; real calls disabled | DPA, data-flow/redaction review, evaluations, budgets, observed model identity and formal approval remain external entry gates | [ADR-0004](adr/ADR-0004-ai-drafts-and-human-commands.md), [Gate 4](evidence/gate4/gate4-summary.md), [current handoff](HANDOFF.md) |
| Object storage for Observer evidence | Observer | Provider not selected; no production objects | Gate 3 encryption/key, retention/delete/legal-hold, tenant prefix and hash verification | [data governance](governance/data-governance.md), [Observer boundary](../services/observer/README.md) |
| Malware scanning/quarantine | Security | Required capability not wired in Gate 0/1 | Gate 3 upload tests and incident evidence | [threat model](governance/threat-model.md) |
| Context/Decision Service | Context | Gate 3–4 local runtime, provenance, temporal context, conflicts and immutable Decisions verified with synthetic data | Production retention, privacy approval and real source quality remain external gates | [Gate 4](evidence/gate4/gate4-summary.md), [v4 design](superpowers/specs/2026-08-06-gbos-v4-agent-context-roadmap-design.md) |
| Agent Runtime | AI platform | Gate 4 durable task, lease, budget, Action Guard and human review verified locally; provider/tool traffic disabled | Provider selection, evaluation, Security Owner and privacy approval remain external gates | [Gate 4](evidence/gate4/gate4-summary.md), [threat model](governance/threat-model.md) |
| Metrics API / governed read model | Analytics | Gate 5 definitions, lineage, freshness, coverage, reconciliation, fail-closed API and CEO cockpit verified with synthetic data | Live source canary, preproduction reconciliation, governance-owner sign-off and UAT remain external gates | [Gate 5](evidence/gate5/gate5-summary.md), [metric registry](../contracts/gate5/metrics-registry-v1.json) |
| Gate 6 release, operations and recovery controls | Release/SRE | Local manifest contract, fail-closed preflight, dry-run plan, 10 SLOs, 16 alerts, 12 runbooks and local MariaDB/PostgreSQL restore parity verified; no live executor exists | Approved production topology, secret store, monitoring destination, regional backup/PITR drill and two-person production authorization remain external gates | [Gate 6](evidence/gate6/gate6-summary.md), [release decision](evidence/gate6/release-decision.json) |
| Gate 6 privacy operations | Privacy/Legal | Seven schemas and six synthetic examples verify fail-closed workflow mechanics | Applicable legal basis, notices, recipient assessment, real data inventory and formal privacy/cross-border approvals remain `blocked_external_input` | [privacy operations](governance/gate6/privacy-operations.md), [Gate 6 decision](evidence/gate6/release-decision.json) |
| Secrets and key management | Security/Platform | 本地 runtime 已实现 macOS Keychain reference → 私有临时目录 mode `0600` 文件；仓库、site config 和日志不保存明文 secret | 当前仍缺真实 Email/DeepSeek/lexicon 值；production rotation、访问审计、恢复与 Kingdee/preproduction keys 保持外部门禁 | [local runbook](local-pilot/RUNBOOK.md), [threat model](governance/threat-model.md) |
| Formal local pilot composition | Platform/AI/Observer | Formal manifest remains `production_go=false`, `local_pilot_go=false`, `composition.status=not_composed`；最终镜像 synthetic core、原生 Frappe、identity metrics live scrape 均已观察，但 channels/models/media/tunnel 仍关闭 | Real channels、provider API call/identity、72 小时试点、Kingdee、cloud 和 production remain unverified and No-Go | [local-pilot manifest](../infra/local/local-pilot-manifest.json), [runtime entrypoints](../infra/local/runtime-entrypoints.json), [current runtime evidence](evidence/identity-resolution-runtime/identity-resolution-runtime-summary.md) |
| Local-pilot image lock | Platform | `infra/local/images.lock.json` records Frappe/PWA inspect digest `sha256:d9220d580ea36fdc04efbe9e11863f2bfb89d879255f52d6af838ee7c0b3cea5` and local-runtime inspect digest `sha256:ceaf2daa0a578698c5f0a2df2d94030b84439b78c9e4a1e73110c4e1a3cf2aae`, both built from revision `098d728` | A recorded local digest is not a formal composition Go, production image, real-channel proof, or 72-hour pilot; rebuilds require a new source-bound evidence package | [image lock](../infra/local/images.lock.json), [current runtime evidence](evidence/identity-resolution-runtime/identity-resolution-runtime-summary.md) |

No row authorizes production. Changing an owner, status, gate, or evidence link
requires a reviewed change and must not introduce credentials or raw business
exports into the repository.

## Gate 2 capability ledger

该表只记录 Gate 2 的真实能力状态。`not_started` 表示后续 Gate 仍需单独
实现和验证；`not_applicable` 表示该能力不属于 Gate 2 的可执行范围。

| Capability | Gate 2 status | Owner | Required next evidence |
|---|---|---|---|
| real connector | `not_started` | Observer | Gate 3 provider authorization, replay, consent and isolation tests |
| real model | `not_started` | AI governance | DeepSeek gateway is configured, but no real call/model identity is observed; Gate 3/4 DPA, redaction, evaluation, budget and human-review evidence remain required |
| production channel | `not_started` | Observer/Privacy | Gate 3 approved channel, account and retention evidence |
| Kingdee live access | `not_started` | ERP integration | Gate 5 least-privilege auth, metadata, read query and audit evidence |
| cloud runtime | `not_started` | Platform/Security | Gate 5 Singapore preproduction security, privacy and recovery evidence |
| production deployment | `not_applicable` | Release owner | Gate 6 Go/No-Go, monitoring, backup/DR, UAT and rollback evidence |

Gate 2 只验证 design/schema/synthetic example/mock。所有真实凭据、网络、
业务数据、外部 writer、云部署和 production 开关保持关闭。
