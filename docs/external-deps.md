# 外部依赖与 Gate 证据

状态仅描述当前仓库的锁定/开关，不代表供应商合规、服务可用或生产已
接通。`blocking gate` 是放行前必须满足的条件；`evidence` 链接工程
记录或明确标注待补证据。`production disabled` by default；生产采集、真实模型和
真实金蝶访问默认关闭。当前身份解析 roadmap 的来源基线是 `8c40731`；2026-08-27 审计
HEAD 为 `7d97e3dc9f9626e9d4570d6b3509152f6ba0d5b7`。image lock 的历史 Frappe source
reference 为 `35beb2586f12043ce4b89b6875527ec4a75150b9`，runtime source reference 为
`1fd20d4df930fc9a70168453d29be1c9dc192522`，recording commit 为
`54d9aa7866189d5fe2028aeea177f6cff8102b41`。锁定 source SHA256 labels 为
`f6fe3ab3938890e6d041df03bfd5857528c8e1269a631b38d6bbb527978c959d` 与
`e946cdf903d87b9d387107b82801556ad85994cb7e5702c21854eebda804fd3e`；当前 runtime
source 已变为 `ae14e6c56ab2cd5c2fa5324bb7bc7364ba84bea2b6cf110f74ca0ac3705aa0fc`，因此
local-runtime 镜像过期。当前证据与历史快照的边界见
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
| Gate 0/1 High/Critical exceptions | Security | Historical Trivy filesystem/locked-image scans 曾 exit `0`，记录 `0` 个未豁免 High/Critical、`0` image secrets、`0` misconfigurations；57 条 waiver 覆盖 103 个 exact PURL，均于 2026-09-30 到期，不宣称当前或 total findings 为零 | 当前 runtime 源码晚于锁定镜像；最终代码与重建镜像必须重新执行 Trivy/Gitleaks。扫描通过也不自动转成生产批准，豁免到期、镜像重建或数据库更新都需独立签字 | [security exceptions](governance/security-exceptions-gate01.md), [historical credential-free closure](evidence/user-identity-governance-closure/identity-governance-summary.md) |
| Kingdee K3Cloud/MCP read connector | ERP integration | Gate 5 exact read-only adapter and synthetic transport implemented; live transport disabled; no write tool exists | Restricted `AI_ReadOnly` account, approved destination, consent/data review and startup → auth → metadata → business-query canary evidence remain `blocked_external_input` | [ADR-0003](adr/ADR-0003-kingdee-v1-read-only.md), [Gate 5](evidence/gate5/gate5-summary.md) |
| Channel connectors (email, WeCom, WhatsApp, phone, meeting, file, manual import) | Observer | Provider-neutral Email Gateway backend、opaque mailbox identity、routing/SLA/draft/retention 的离线基础已实现；PWA 仍为 partial/synthetic-harness verified。WeCom Task 6 已冻结；Tasks 7–8 部分完成，Task 9 未开始。production ingestion disabled，真实 Email/WeCom login/checkpoint/canary 均为 0 | Provider authorization、activation-time/checkpoint、retention/legal-hold、malware 和 tenant-isolation 按 connector 单独放行。`errcode=45009` 已批准为单邮箱持久暂停、禁止自动恢复、只允许管理员人工恢复；但 typed pause、checkpoint 与 admin resume 的 runtime bridge 尚未实现。webhook/poller/reconciler/Compose/tunnel/secrets/E2E 与全绿回归均是 P0 | [Observer boundary](../services/observer/README.md), [WeCom compatibility note](compat/wecom-app-mail-contract.md), [current handoff](HANDOFF.md) |
| DeepSeek model gateway | AI governance | Gateway implementation and configuration are present for `https://api.deepseek.com` / `deepseek-v4-flash`; deterministic Gate 4 agent and review path are implemented, but no real call or response model identity has been observed; real calls disabled | DPA, data-flow/redaction review, evaluations, budgets, observed model identity and formal approval remain external entry gates | [ADR-0004](adr/ADR-0004-ai-drafts-and-human-commands.md), [Gate 4](evidence/gate4/gate4-summary.md), [current handoff](HANDOFF.md) |
| Object storage for Observer evidence | Observer | Provider not selected; no production objects | Gate 3 encryption/key, retention/delete/legal-hold, tenant prefix and hash verification | [data governance](governance/data-governance.md), [Observer boundary](../services/observer/README.md) |
| Malware scanning/quarantine | Security | Required capability not wired in Gate 0/1 | Gate 3 upload tests and incident evidence | [threat model](governance/threat-model.md) |
| Context/Decision Service | Context | Gate 3–4 local runtime, provenance, temporal context, conflicts and immutable Decisions verified with synthetic data | Production retention, privacy approval and real source quality remain external gates | [Gate 4](evidence/gate4/gate4-summary.md), [v4 design](superpowers/specs/2026-08-06-gbos-v4-agent-context-roadmap-design.md) |
| Agent Runtime | AI platform | Gate 4 durable task, lease, budget, Action Guard and human review verified locally; provider/tool traffic disabled | Provider selection, evaluation, Security Owner and privacy approval remain external gates | [Gate 4](evidence/gate4/gate4-summary.md), [threat model](governance/threat-model.md) |
| Metrics API / governed read model | Analytics | Gate 5 definitions, lineage, freshness, coverage, reconciliation, fail-closed API and CEO cockpit verified with synthetic data | Live source canary, preproduction reconciliation, governance-owner sign-off and UAT remain external gates | [Gate 5](evidence/gate5/gate5-summary.md), [metric registry](../contracts/gate5/metrics-registry-v1.json) |
| Gate 6 release, operations and recovery controls | Release/SRE | Local manifest contract, fail-closed preflight, dry-run plan, 10 SLOs, 16 alerts, 12 runbooks and local MariaDB/PostgreSQL restore parity verified; no live executor exists | Approved production topology, secret store, monitoring destination, regional backup/PITR drill and two-person production authorization remain external gates | [Gate 6](evidence/gate6/gate6-summary.md), [release decision](evidence/gate6/release-decision.json) |
| Gate 6 privacy operations | Privacy/Legal | Seven schemas and six synthetic examples verify fail-closed workflow mechanics | Applicable legal basis, notices, recipient assessment, real data inventory and formal privacy/cross-border approvals remain `blocked_external_input` | [privacy operations](governance/gate6/privacy-operations.md), [Gate 6 decision](evidence/gate6/release-decision.json) |
| Secrets and key management | Security/Platform | 本地 Mac 试点的 macOS Keychain 保持 local-only；未来 deployment 方案已选为 `planned_tencent_tke_oidc_ssm_external_secrets`：Tencent Cloud SSM pinned version + TKE ServiceAccount OIDC/CAM 临时角色 → External Secrets → KMS 加密的 Kubernetes Secret → 启动投影到内存卷 0400 普通文件 → 只读 `/run/secrets` → `MountedFileSecretProvider`。`adapter_implementation=not_started`；no Tencent Cloud resource has been created | 当前仍缺腾讯云区域/账号批准、真实 TKE workload identity/SSM versions、适配器实现、稳定 preflight、轮换/60 分钟回滚演练与 separate approvals。Production Go remains false；仓库、环境变量、argv、镜像、日志、Frappe site config、审计值/哈希均不得承载明文 secret | [deployment lifecycle](deployment-secrets.md), [Tencent TKE design](superpowers/specs/2026-08-11-gbos-tencent-tke-secret-projection-design.md), [projection template](../infra/prod/secret-provider-v1.template.json), [threat model](governance/threat-model.md) |
| Formal local pilot composition | Platform/AI/Observer | Checked-in topology remains `composition.status=composed` and all channels/models/sends/deletion are disabled. On 2026-08-27 Docker/OrbStack and locked images were unavailable; formal/synthetic preflight returned `rc78` for multiple blockers, not solely `local_pilot_go=false`. The `test:e2e:site` result `4 passed, 21 skipped, 0 failed` in `6.5s` is historical-only and **not rerun on the current source-bound images**; the prior snapshot was not all 25 live | Fix Docker/image availability, port `55432`, Email Gateway launcher profiles, runtime source/image mismatch and WeCom runtime red before any real channel. Real channels、provider API call/identity、fault drills、Kingdee、cloud 和 production remain unverified and No-Go；72-hour continuity is deferred | [local-pilot manifest](../infra/local/local-pilot-manifest.json), [runtime entrypoints](../infra/local/runtime-entrypoints.json), [current handoff](HANDOFF.md) |
| Local-pilot image lock | Platform | `infra/local/images.lock.json` historically records Frappe/PWA inspect digest `sha256:0b0e24d7e25c2e384e977c1aa00ef8d032e54aadbb84af813fb077c58fd28460` at Frappe source `35beb25` and local-runtime inspect digest `sha256:489ad22e95300ec27156904d583f67979cf8142f8b31479d8b938ad3d3a6c0b1` at runtime source `1fd20d4`. Current Frappe source still matches; current runtime source does not | Rebuild the final clean runtime source, refresh image lock, inspect labels/digests and attest running containers. Historical digests do not prove current HEAD, formal Go, real channel/model, terminal deletion or production readiness | [image lock](../infra/local/images.lock.json), [historical credential-free closure](evidence/user-identity-governance-closure/identity-governance-summary.md) |

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
