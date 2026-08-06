# 外部依赖与 Gate 证据

状态仅描述当前仓库的锁定/开关，不代表供应商合规、服务可用或生产已
接通。`blocking gate` 是放行前必须满足的条件；`evidence` 链接工程
记录或明确标注待补证据。生产采集、真实模型和真实金蝶访问默认关闭。

| 依赖/能力 | Owner | Status | Blocking gate | Evidence |
|---|---|---|---|---|
| Frappe Framework `v16.30.0` | Platform | Frozen; fresh three/four-App installs and migrations verified locally | Gate 0 complete; upgrade requires a new compatibility gate | [compatibility matrix](compat/compatibility-matrix.md), [versions](compat/versions.json), [Gate 0](evidence/gate0-summary.md) |
| ERPNext `v16.31.0` | Platform | Frozen; installed and transaction guard verified locally | Gate 0 complete; ERPNext transactional UI remains disabled in V1 | [compatibility matrix](compat/compatibility-matrix.md), [Gate 1](evidence/gate1-summary.md) |
| Frappe CRM `v1.81.0` | Platform | Frozen; metadata contract and runtime verified locally | License review remains blocking before external service | [compatibility matrix](compat/compatibility-matrix.md), [license baseline](compat/license-baseline.md) |
| `frappe_docker` `v3.2.2` | Platform | Frozen; exact source build and healthy core stack verified | Rebuild only from locked source and clean commit | [compatibility matrix](compat/compatibility-matrix.md), [image build](compat/image-build.md) |
| MariaDB `11.8` image | Platform | Digest locked; health plus backup/restore verified locally | Production backup, PITR and DR remain Gate 5/6 | [versions](compat/versions.json), [Gate 1](evidence/gate1-summary.md) |
| PostgreSQL 17 + pgvector `0.8.2` image | Observer | Digest locked; optional Gate 0/1 connectivity/contract check passed | Gate 3 Observer schema, vector indexes and retention validation | [versions](compat/versions.json), [Observer boundary](../services/observer/README.md) |
| Redis `6.2-alpine` image | Platform | Digest locked; cache, queue, worker and scheduler health verified locally | Production HA/monitoring remains Gate 5/6 | [versions](compat/versions.json), [Gate 1](evidence/gate1-summary.md) |
| Python `3.14.2` / Node `24.13.0` | Platform | Exact runtime verified; final Node scope reduced to realtime dependencies | Upgrade requires compatibility, scan and regression evidence | [compatibility matrix](compat/compatibility-matrix.md), [Gate 1](evidence/gate1-summary.md) |
| Gate 0/1 High/Critical exceptions | Security | 57 expiring entries / 103 exact PURLs; 0 unwaived findings | Gate 5/6 blocked until remediation or independent production approval | [security exceptions](governance/security-exceptions-gate01.md), [Gate 1](evidence/gate1-summary.md) |
| Kingdee K3Cloud/MCP read connector | ERP integration | Gate 1 mock/fixture only; production disabled | Gate 2 freezes mapping/contracts/mock with zero network; Gate 5 alone may run a read-only canary after consent/data review, least-privilege auth and audit; no write tool | [ADR-0003](adr/ADR-0003-kingdee-v1-read-only.md), [ADR-0009](adr/ADR-0009-four-truths-agent-context-and-gate-sequencing.md) |
| Channel connectors (email, WeCom, WhatsApp, phone, meeting, file, manual import) | Observer | Production ingestion disabled | Gate 3 per-connector auth, replay, consent, retention, rate/size, tenant-isolation tests | [Observer boundary](../services/observer/README.md), [event contract](../contracts/canonical-observation-event.schema.json) |
| Real AI/ASR model provider | AI governance | Not selected; real calls disabled | Gate 3 may approve bounded tool-free transcription/summary/extraction after DPA/data-flow/redaction review; Gate 4 separately requires Agent routing, budget, evaluation and human-review proof | [ADR-0004](adr/ADR-0004-ai-drafts-and-human-commands.md), [threat model](governance/threat-model.md) |
| Object storage for Observer evidence | Observer | Provider not selected; no production objects | Gate 3 encryption/key, retention/delete/legal-hold, tenant prefix and hash verification | [data governance](governance/data-governance.md), [Observer boundary](../services/observer/README.md) |
| Malware scanning/quarantine | Security | Required capability not wired in Gate 0/1 | Gate 3 upload tests and incident evidence | [threat model](governance/threat-model.md) |
| Context/Decision Service | Context | Design only; no runtime | Gate 2 contract/ontology design; Gate 3 provenance/temporal minimum; Gate 4 conflicts, decisions and Agent timeline | [ADR-0009](adr/ADR-0009-four-truths-agent-context-and-gate-sequencing.md), [v4 design](superpowers/specs/2026-08-06-gbos-v4-agent-context-roadmap-design.md) |
| Agent Runtime | AI platform | Not implemented; real tool/model traffic disabled | Gate 4 durable task, lease, budget, sandbox, Action Guard, evaluation and recovery evidence | [ADR-0009](adr/ADR-0009-four-truths-agent-context-and-gate-sequencing.md), [threat model](governance/threat-model.md) |
| Metrics API / governed read model | Analytics | Not implemented; Gate 1 dashboard is synthetic demo data | Gate 2 metric contracts; Gate 5 definitions, lineage, freshness, coverage, reconciliation, fail-closed API and CEO cockpit | [ADR-0009](adr/ADR-0009-four-truths-agent-context-and-gate-sequencing.md), [v4 design](superpowers/specs/2026-08-06-gbos-v4-agent-context-roadmap-design.md) |
| Secrets and key management | Security/Platform | Runtime mechanism not committed | Gate 3 Observer keys; Gate 4 model/Agent keys; Gate 5 Kingdee/MCP/preproduction keys; Gate 6 production rotation, access audit and recovery | [permission matrix](permission-matrix.md), [threat model](governance/threat-model.md) |

No row authorizes production. Changing an owner, status, gate, or evidence link
requires a reviewed change and must not introduce credentials or raw business
exports into the repository.

## Gate 2 capability ledger

该表只记录 Gate 2 的真实能力状态。`not_started` 表示后续 Gate 仍需单独
实现和验证；`not_applicable` 表示该能力不属于 Gate 2 的可执行范围。

| Capability | Gate 2 status | Owner | Required next evidence |
|---|---|---|---|
| real connector | `not_started` | Observer | Gate 3 provider authorization, replay, consent and isolation tests |
| real model | `not_started` | AI governance | Gate 3/4 DPA, redaction, evaluation, budget and human-review evidence |
| production channel | `not_started` | Observer/Privacy | Gate 3 approved channel, account and retention evidence |
| Kingdee live access | `not_started` | ERP integration | Gate 5 least-privilege auth, metadata, read query and audit evidence |
| cloud runtime | `not_started` | Platform/Security | Gate 5 Singapore preproduction security, privacy and recovery evidence |
| production deployment | `not_applicable` | Release owner | Gate 6 Go/No-Go, monitoring, backup/DR, UAT and rollback evidence |

Gate 2 只验证 design/schema/synthetic example/mock。所有真实凭据、网络、
业务数据、外部 writer、云部署和 production 开关保持关闭。
