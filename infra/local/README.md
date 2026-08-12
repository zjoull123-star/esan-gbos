# Local pilot composition

This directory declares an isolated, default-off local topology. Its service
composition state is `composed`: the topology is implemented and the current-source
runtime and Frappe images are built and recorded. The checked-in manifest still keeps
`local_pilot_go=false`, and `scripts/local-pilot/start` must fail before secret
materialization or container actions (`preflight --require-go` returns 78).
Composition therefore does not authorize a real start, external system, or production.

PostgreSQL migrations run once per invocation in the frozen
observer → context → agent → media order. An immutable checksum ledger makes a
second invocation a no-op and rejects changed migration content. Runtime
services connect with separate `NOBYPASSRLS` roles.

The local evidence truth is the named filesystem CAS volume
`local-pilot-evidence-cas`. Content-address validation and collision rejection
provide the immutability boundary. MinIO is not part of the required runtime
because no service is wired to it.

All checked-in configuration contains file references only. Runtime secret
values are read from macOS Keychain into private `0600` files under a temporary
directory, mounted at `/run/secrets`, and removed by a normal stop. Named data
volumes are preserved.

Observer, Context, Agent, and Media use four different PostgreSQL password
files and Keychain accounts. On the target OrbStack host, a probe confirmed
that a host uid 501 mode-`0600` bind is visible as uid 10001 mode `0600` and is
readable by runtime user `10001`; this is host-specific evidence and must be
rechecked on another container runtime.

The Frappe, MariaDB, Redis, backend, worker, scheduler, websocket, and PWA
services use local-pilot-specific volume names. The Frappe services use the
recorded local image `esan-gbos-local-pilot-frappe:2026-08-08` with inspect digest
`sha256:2a0440df614314dec036ecc934e37aa0b3713b8cb8610e3ca2bd8ed69f9187c2`;
the local runtime digest is
`sha256:de037ad28a020689fec8b72f743ad0224afdf5c2ca6856a2ea5568fabd45e568`.
The Frappe source reference is
`485d3def0ea30ee49a3899d71c10b0787ba0429f`; the runtime source reference is
`bb260632ff44c7065a88327f264612139a9070a2`; the image-lock recording commit is
`a599a5200e2a8e1b5e42301d74fe8d9d914161c4`. Their source SHA256 labels are
`441e33dec9acd744dd1b461ae49e950d18f764f05ae74e90357091a698320405` and
`c23d41903977fb350764ceee8a21efad70ce1079a7b6eed4503a87af3ac37db3`; each image label
and source hash was inspected independently.
the upstream ERPNext image is not treated as a GBOS PWA. The previous synthetic
snapshot, rather than this rebuild alone, reported
`setup_complete=1`, Frappe/ERPNext/CRM/esan_gbos versions
`16.30.0`/`16.31.0`/`1.81.0`/`0.1.0`, and `bench migrate` was checksum-consistent
across two runs. The optional `frappe-synthetic-bootstrap` profile reads a
repository-external test-user password from `/run/secrets/frappe_demo_password`
and calls only the governed synthetic fixture seed. The snapshot ran
`scripts/local-pilot/bootstrap-synthetic-user --acknowledge-synthetic`, and
Playwright verified `/gbos/ceo`; the helper still reruns the real go/image/
composition preflight and refuses to start missing dependencies implicitly.

After PostgreSQL migrations and Frappe site setup, `start` runs the profile-only
`frappe-materializer-bootstrap` before the runtime services. It reads the
materializer API key and secret from mode-`0600` files without logging them and
invokes the bench-only provisioning helper for the exact closed service
identity. The synthetic snapshot records materializer bootstrap as skipped/idempotent;
formal materializer identity provisioning remains behind the formal No-Go gate.

Published PostgreSQL, MariaDB, API, PWA, webhook, and optional monitoring ports
bind only to `127.0.0.1` (synthetic snapshot: PWA `58080`, Context `58001`, Agent
`58002`, Observer `58003`, PostgreSQL `55432`, MariaDB `53306`). `local-internal`
is a bridge with `enable_ip_masquerade=false`, not `internal: true`; pwa/context/
agent/observer access to `api.deepseek.com:443` was blocked. `webhook-tunnel` remains
internal, profile-gated, has no API network membership, and routes only to WhatsApp
webhook ingress.

`scripts/local-pilot/preflight --synthetic` validates only the checked-in,
fully disabled smoke configuration. It is mutually exclusive with
`--require-go` and does not waive the real composition, image, or governance
gates.

With the required local images built, recorded, and inspected,
`scripts/local-pilot/start-synthetic --acknowledge-synthetic` renders a
temporary core-only manifest and starts the three runtime APIs plus Frappe/PWA.
It never enables connector, model, media, or tunnel profiles and does not alter
the checked-in manifest, composition state, or formal `start --require-go` gate.
The earlier synthetic snapshot completed this path with its then-source-bound images;
the newly recorded source-bound images have not been promoted to real-channel evidence.
The pinned Prometheus profile also observed the authenticated
`identity-resolution` target as `up=1` with five healthy rules. Resolver
readiness remained `0` because the real identity worker and channels were
disabled. This is local synthetic evidence, not a formal Go. The 72-hour window is
deferred by user decision, is not assessed, and is no longer an exit requirement.

The current-source synthetic core restart is separate from formal pilot approval:
`local_pilot_go=false`, with channels, models, media and tunnel still disabled; the core is
healthy on the current locked images. Formal preflight returns `rc78` solely because
`local_pilot_go=false`. The final credential-free P0 run records full backend
`3064 passed, 44 skipped, 1 warning`, failed `0`; the warning is the existing Starlette
TestClient/httpx deprecation. Ruff check/format, mypy, compileall and `scripts/dev/secret-scan`
are green; format covers `528 files` and mypy `101 sources`. Frontend lint/typecheck/build are
green, unit `197 passed`, and frontend-harness Playwright `25 passed`. A disposable no-volume
pgvector Gate 3 run recorded 15 migration-ledger entries, applied migrations twice, passed
`17` integration tests with one existing warning, and removed its container. Full-history
Gitleaks scanned `263 commits` with `0 leaks` under the reviewed exact synthetic allowlist
committed at `c27687ec6b39e669014b9ae8980cf6565556aaba`; this is not an unreviewed zero claim.
Current-image live-site Playwright `test:e2e:site` at `http://127.0.0.1:58080` completed in
`6.5s`: `4 passed, 21 skipped, 0 failed`. Applicable live scopes were five role workspaces axe,
CEO cockpit governance/source values, keyboard skip/nav order, and
integrations+communications Restricted/3 viewports. The 21 skipped scenarios were
harness-only by design; this is not all 25 live. The repo-external `0600` synthetic CEO
storage state was sourced in-process from Keychain, and temporary auth state/test-results
were deleted afterward.
Trivy filesystem and current locked-image scans exited `0`, with only `0` unwaived
High/Critical, `0` image secrets and `0` misconfigurations reported. The historical waiver
set has `57` entries covering `103` exact PURLs expiring `2026-09-30`; total findings are not
claimed zero. Email IMAP login/checkpoint/canary remains unrun because working client
authorization is missing, DeepSeek response-reported model remains `unknown`, and formal
`production_go=false`/`local_pilot_go=false` remain unchanged.

The earlier `3060 passed, 44 skipped, 3 failed` result remains recorded in current closure
evidence as stale-current-doc mismatch only; the final run above closed that mismatch.

Build the runtime image explicitly with
`scripts/local-pilot/build-runtime-image --confirm-network-build`. Python and
uv use fixed linux/arm64 digest references, but the explicit flag is still
required because retrieving those images and frozen Python dependencies may
use network access. The command atomically records their inspected IDs,
RepoDigests, and platforms only after a successful build.
Build the GBOS Frappe/PWA image separately with
`scripts/local-pilot/build-frappe-image --confirm-network-build`; the explicit
flag acknowledges that the governed upstream builder verifies remote tags. The
current lock entry is the digest recorded above; a later rebuild must produce a
new evidence snapshot rather than silently reusing this one.
