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
| Kingdee K3Cloud/MCP read connector | ERP integration | Mock/fixture only; production disabled | Gate 3 read-only canary, consent/data review, least-privilege auth, audit; no write tool | [ADR-0003](adr/ADR-0003-kingdee-v1-read-only.md), [threat model](governance/threat-model.md) |
| Channel connectors (email, WeCom, WhatsApp, phone, meeting, file, manual import) | Observer | Production ingestion disabled | Gate 3 per-connector auth, replay, consent, retention, rate/size, tenant-isolation tests | [Observer boundary](../services/observer/README.md), [event contract](../contracts/canonical-observation-event.schema.json) |
| Real AI model provider | AI governance | Not selected; real calls disabled | Gate 3 provider/DPA/data-flow review, model evaluation, redaction, budget, human-review proof | [ADR-0004](adr/ADR-0004-ai-drafts-and-human-commands.md), [threat model](governance/threat-model.md) |
| Object storage for Observer evidence | Observer | Provider not selected; no production objects | Gate 3 encryption/key, retention/delete/legal-hold, tenant prefix and hash verification | [data governance](governance/data-governance.md), [Observer boundary](../services/observer/README.md) |
| Malware scanning/quarantine | Security | Required capability not wired in Gate 0/1 | Gate 3 upload tests and incident evidence | [threat model](governance/threat-model.md) |
| Secrets and key management | Security/Platform | Runtime mechanism not committed | Gate 3 rotation, access audit, redaction, recovery test | [permission matrix](permission-matrix.md), [threat model](governance/threat-model.md) |

No row authorizes production. Changing an owner, status, gate, or evidence link
requires a reviewed change and must not introduce credentials or raw business
exports into the repository.
