# Gate 0 compatibility matrix

Evidence date: 2026-08-06. The source references below are immutable. Runtime
claims are limited to the disposable local ARM64 environment described here.

| Component | Frozen release | Commit or image digest | License | Local result |
|---|---|---|---|---|
| Frappe Framework | `v16.30.0` | `9523516cac25992bc2cd810e1015df8994c257f5` | MIT | Installed; `bench version` reported `16.30.0` |
| ERPNext | `v16.31.0` | `68ea583a1fbd1c533004cabc4294213e9a58716e` | GPL-3.0 | Installed; `bench version` reported `16.31.0` |
| Frappe CRM | `v1.81.0` | `f6016eab20936ea15e5f450ec8dff9880f4dffe9` | AGPL-3.0 | Installed; `bench version` reported `1.81.0` |
| frappe_docker | `v3.2.2` | `3061850feface8fbbad15b5dc08a110c596107cb` | MIT | Custom image build source |
| Gate 0 upstream image | `esan-gbos-upstream:gate0` | `sha256:b69f0001225523ec52ceb6d80fc696c34f24c560a0d15c5ebc53e803eb5286ec` | Combined upstream obligations | Built and run on ARM64 |
| `esan_gbos` | Gate 1 local monorepo app | Final digest pending | Private / licensing decision pending | **Not included in the verified Gate 0 upstream image** |

Infrastructure image indexes remain frozen in
[`versions.json`](versions.json). Moving branches and floating image tags are
not accepted build or smoke-test inputs.

## Verified local upstream runtime

The local host used OrbStack 2.2.2, Docker 29.4.0, Compose 5.1.2, and buildx
0.33.0 on ARM64. The custom image was created at
`2026-08-06T03:00:09+08:00` for `linux/arm64`.

- Backend Python: 3.14.2.
- Node.js 24.13.0: builder stage only; it is intentionally absent from the
  backend runtime stage.
- MariaDB 11.8 and Redis 6.2 Alpine supported the Frappe site. PostgreSQL 17
  Bookworm with pgvector 0.8.2 is digest-pinned and isolated to the optional
  Observer profile; its extension/version healthcheck is separate evidence.
- A fresh `gbos.localhost` site installed Frappe and ERPNext, then CRM.
  `bench --site gbos.localhost list-apps --format json` returned exactly
  `frappe`, `erpnext`, and `crm`.
- Two consecutive `bench --site gbos.localhost migrate` commands exited 0.

This verifies the three upstream apps together. It does not verify the Gate 1
final image or the four-app install.

## Findings retained as evidence

- The first exact-version custom build stopped making progress while Yarn
  downloaded `sortablejs`, `vuex`, and `yargs`. Host, Alpine, and the same
  Node/Yarn network path remained reachable. The hung build was terminated and
  an exact-input retry succeeded. This is recorded as a transient network hang,
  not as a compatibility failure. The build script retries once without
  changing refs.
- The backend healthcheck initially received HTTP 404 because it omitted the
  Frappe site `Host` header. The regression fix sends
  `Host: gbos.localhost`; the backend subsequently became Healthy.
- The upstream backend-stage image contains `curl` and Python but not `wget`.
  The frontend healthcheck therefore uses `curl`; after recreation the
  upstream frontend container reported Healthy.
- Frappe logs contain an upstream warning that `duckdb_sync` method
  `cleanup_old_syncs` is invalid. Both migrations still exited 0. This warning
  remains a known limitation to investigate; it is not silently treated as a
  successful check.

## Frozen CRM DocType contract

[`crm-doctype-contract.json`](crm-doctype-contract.json) records the
runtime-inspected parent and child-table contract for CRM v1.81.0. Validate the
snapshot with:

```bash
scripts/dev/verify-crm-contract
```

For a live refresh in the disposable stack, run each locked DocType through
the same API and compare the returned metadata before changing the snapshot:

```bash
docker compose --env-file infra/dev/.env -f infra/dev/compose.yml exec -T backend \
  bench --site gbos.localhost execute frappe.get_meta --args '["CRM Organization"]'
```

Repeat for `CRM Lead`, `CRM Deal`, and `CRM Contacts`.

## Gate 1 final-image gate

Gate 1 must produce a clean, committed `apps/esan_gbos` source tree and build
the local monorepo app on top of the verified upstream image. The final smoke
must prove a fresh site with exactly `frappe`, `erpnext`, `crm`, and
`esan_gbos`, followed by two successful migrations, final-image security and
license scans, and a CycloneDX SBOM.

No fallback to Frappe/ERPNext v15 is permitted. Failure of that final smoke is a
blocker; the three-app Gate 0 evidence cannot be promoted into a four-app claim.

## Sources

- [Frappe v16.30.0](https://github.com/frappe/frappe/releases/tag/v16.30.0)
- [ERPNext v16.31.0](https://github.com/frappe/erpnext/releases/tag/v16.31.0)
- [Frappe CRM v1.81.0](https://github.com/frappe/crm/releases/tag/v1.81.0)
- [frappe_docker v3.2.2](https://github.com/frappe/frappe_docker/releases/tag/v3.2.2)
