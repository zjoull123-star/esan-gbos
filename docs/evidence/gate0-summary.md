# Gate 0 evidence summary

Status: **pass for the local disposable Gate 0 boundary**.

Go/No-Go: **Gate 0 Go**. This is not a production approval. Gate 5
preproduction and Gate 6 production remain **No-Go**.

## Immutable compatibility evidence

- Host: OrbStack 2.2.2; Docker 29.4.0; Compose 5.1.2; buildx 0.33.0;
  ARM64.
- Frappe Framework `v16.30.0` at
  `9523516cac25992bc2cd810e1015df8994c257f5`.
- ERPNext `v16.31.0` at
  `68ea583a1fbd1c533004cabc4294213e9a58716e`.
- Frappe CRM `v1.81.0` at
  `f6016eab20936ea15e5f450ec8dff9880f4dffe9`.
- `frappe_docker` `v3.2.2` at
  `3061850feface8fbbad15b5dc08a110c596107cb`.
- The original upstream-only ARM64 image remains recorded at
  `sha256:b69f0001225523ec52ceb6d80fc696c34f24c560a0d15c5ebc53e803eb5286ec`.
  A fresh site installed exactly `frappe`, `erpnext`, and `crm`; two
  consecutive migrations exited 0.
- The Gate 1 foundation image was built from committed runtime source
  `deccc2caaa2d25cebceab2aff99dbbbb4e037a04` and has local ARM64 digest
  `sha256:a55e3dc432cabc7e4a1bbe4951d1586c97e65151b41a5d9c7e5eb0632d61f1e9`.
  Its fresh site installed exactly `frappe`, `erpnext`, `crm`, and
  `esan_gbos`.

The CRM metadata snapshot and all JSON Schema 2020-12 examples pass the
repository contract checks. PostgreSQL 17 with pgvector 0.8.2 also passed the
optional Observer connectivity/contract check; Observer remains isolated and
is not a Gate 1 runtime dependency.

## Security and governance result

- Repository Trivy, secret, and misconfiguration scans reported no blocking
  result.
- The final-image scan exited 0 with **0 unwaived High/Critical** findings.
- The remaining 85 Debian and 18 Python scanner findings are not hidden:
  57 time-bounded Gate 0/1 exceptions cover 103 exact PURLs and expire no
  later than `2026-09-30T00:00:00Z`.
- Those exceptions do not authorize Gate 5/6. The machine policy, owner,
  scope, expiry, remediation conditions, and production block are documented
  in [Gate 0/1 security exceptions](../governance/security-exceptions-gate01.md).
- Production channel ingestion, real model calls, Kingdee networking, external
  writes, and production deployment remained disabled; observed calls for
  each were 0.

The ADRs, permission matrix, data classification, consent/withdrawal,
retention/deletion/export/legal-hold process, Singapore cross-border checklist,
threat model, external dependencies, license baseline, and SBOM workflow are
present and validated.

## Known limitations

- Frappe emits an upstream `duckdb_sync.cleanup_old_syncs` warning during
  migration. Both consecutive migrations and the post-restore migration exit
  0. The warning remains visible and is not represented as fixed.
- The first exact upstream build experienced a transient network hang while
  fetching frontend packages. An unchanged retry succeeded; no tag, commit,
  runtime, or version fallback occurred.
- GitHub returned HTTP 403 for private-repository rulesets because the account
  lacks the required GitHub Pro capability. The repository remains private.
  The compensating control is main-agent-only merge plus green local and
  GitHub CI evidence; the exception is not silently treated as a ruleset.
- AGPL/GPL source and notice obligations still require legal confirmation
  before an external pilot or production service.

## Evidence handling

Compact summaries, machine records, screenshots, and checksums are committed.
Raw logs, scan output, database backups, and multi-megabyte SBOMs remain
outside Git. GitHub workflows upload bounded-retention raw gate logs, license
inventory, CycloneDX SBOM, and checksums. The review surface is
[PR #1](https://github.com/zjoull123-star/esan-gbos/pull/1); its head must be
green before merge.
