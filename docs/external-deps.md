# 外部依赖与 Gate 证据

状态仅描述当前仓库的锁定/开关，不代表供应商合规、服务可用或生产已
接通。`blocking gate` 是放行前必须满足的条件；`evidence` 链接工程
记录或明确标注待补证据。生产采集、真实模型和真实金蝶访问默认关闭。

| 依赖/能力 | Owner | Status | Blocking gate | Evidence |
|---|---|---|---|---|
| Frappe Framework `v16.30.0` | Platform | Frozen; runtime smoke pending | Gate 0 disposable install, health, two migrations | [compatibility matrix](compat/compatibility-matrix.md), [versions](compat/versions.json) |
| ERPNext `v16.31.0` | Platform | Frozen; runtime smoke pending | Gate 0 install/version assertion | [compatibility matrix](compat/compatibility-matrix.md) |
| Frappe CRM `v1.81.0` | Platform | Frozen; runtime smoke pending | Gate 0 install/version assertion and license review before external service | [compatibility matrix](compat/compatibility-matrix.md), [license baseline](compat/license-baseline.md) |
| `frappe_docker` `v3.2.2` | Platform | Frozen; compose evidence exists, runtime pending | Gate 0 digest pull and healthy stack | [compatibility matrix](compat/compatibility-matrix.md) |
| MariaDB `11.8` image | Platform | Digest locked; runtime pending | Gate 0 database health and backup/restore smoke | [versions](compat/versions.json) |
| PostgreSQL 17 + pgvector `0.8.2` image | Observer | Digest locked; optional Gate 0/1 contract/connectivity profile only | Gate 3 Observer schema, vector indexes and retention validation | [versions](compat/versions.json), [Observer boundary](../services/observer/README.md) |
| Redis `6.2-alpine` image | Platform | Digest locked; runtime pending | Gate 0 queue/worker health | [versions](compat/versions.json) |
| Python `3.14.2` / Node `24.13.0` | Platform | Runtime lock recorded | Gate 0 container reports exact runtimes | [compatibility matrix](compat/compatibility-matrix.md) |
| Kingdee K3Cloud/MCP read connector | ERP integration | Mock/fixture only; production disabled | Gate 3 read-only canary, consent/data review, least-privilege auth, audit; no write tool | [ADR-0003](adr/ADR-0003-kingdee-v1-read-only.md), [threat model](governance/threat-model.md) |
| Channel connectors (email, WeCom, WhatsApp, phone, meeting, file, manual import) | Observer | Production ingestion disabled | Gate 3 per-connector auth, replay, consent, retention, rate/size, tenant-isolation tests | [Observer boundary](../services/observer/README.md), [event contract](../contracts/canonical-observation-event.schema.json) |
| Real AI model provider | AI governance | Not selected; real calls disabled | Gate 3 provider/DPA/data-flow review, model evaluation, redaction, budget, human-review proof | [ADR-0004](adr/ADR-0004-ai-drafts-and-human-commands.md), [threat model](governance/threat-model.md) |
| Object storage for Observer evidence | Observer | Provider not selected; no production objects | Gate 3 encryption/key, retention/delete/legal-hold, tenant prefix and hash verification | [data governance](governance/data-governance.md), [Observer boundary](../services/observer/README.md) |
| Malware scanning/quarantine | Security | Required capability not wired in Gate 0/1 | Gate 3 upload tests and incident evidence | [threat model](governance/threat-model.md) |
| Secrets and key management | Security/Platform | Runtime mechanism not committed | Gate 3 rotation, access audit, redaction, recovery test | [permission matrix](permission-matrix.md), [threat model](governance/threat-model.md) |

No row authorizes production. Changing an owner, status, gate, or evidence link
requires a reviewed change and must not introduce credentials or raw business
exports into the repository.
