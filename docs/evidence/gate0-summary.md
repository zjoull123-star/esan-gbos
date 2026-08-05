# Gate 0 evidence summary

Status: **partial**. The exact three-app upstream stack is verified locally.
The Gate 1 final image and final four-app runtime remain pending.

## Verified local Gate 0 upstream evidence

- Host: OrbStack 2.2.2; Docker 29.4.0; Compose 5.1.2; buildx 0.33.0;
  ARM64.
- Image: `esan-gbos-upstream:gate0`, Linux ARM64, created
  `2026-08-06T03:00:09+08:00`, digest
  `sha256:b69f0001225523ec52ceb6d80fc696c34f24c560a0d15c5ebc53e803eb5286ec`.
- Runtime: Python 3.14.2. Node 24.13.0 exists in the builder only and is absent
  from the backend stage.
- `bench version`: Frappe 16.30.0, ERPNext 16.31.0, CRM 1.81.0.
- Fresh site `gbos.localhost`: installed apps were exactly `frappe`, `erpnext`,
  and `crm`.
- Two consecutive migrations exited 0.
- The backend Host-header healthcheck regression was reproduced and fixed.
  The frontend healthcheck was changed from unavailable `wget` to runtime
  `curl`; after recreation, the upstream frontend container reported Healthy.

The first exact build experienced a transient network hang while Yarn fetched
`sortablejs`, `vuex`, and `yargs`. Independent network checks remained
available and the unchanged retry succeeded. This is not classified as an
upstream compatibility failure.

Frappe emitted a `duckdb_sync` warning because `cleanup_old_syncs` is not a
valid method. The migrations still exited 0, but the warning remains an
upstream limitation requiring follow-up.

## Pending Gate 1 evidence

- A deterministic final image built from clean, committed
  `apps/esan_gbos` monorepo source.
- Fresh final four-app runtime: `frappe`, `erpnext`, `crm`, and `esan_gbos`.
- Two consecutive final-image migrations.
- Final-image security scan, license inventory, CycloneDX SBOM, and checksums.
- Vue unit/build gates and Playwright browser checks after the frontend is
  materialized.

The three-app result must not be presented as final four-app runtime evidence.

## Repository ruleset exception

The repository is private and the authenticated viewer has ADMIN permission.
A read-only ruleset query returned HTTP 403 with GitHub's requirement for
GitHub Pro for private-repository rulesets. Repository visibility must remain
private. Until that capability is available, the compensating control is
main-agent-only merge plus required local and CI evidence. Enabling a ruleset
remains an external dependency for the repository owner; no account or
repository setting was changed during this work.

## Evidence handling

Only the compact JSON record, summary, template, and their checksums are
committed. Raw build logs, scan databases, large SBOMs, and secrets are not
stored in the repository. CI uploads scan/SBOM artifacts with bounded
retention.
