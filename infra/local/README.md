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
`sha256:7b9979267b45c0ad8b635581112f245ef635c956a28d4055cfacb59703020d7c`;
the local runtime digest is
`sha256:8a0ac2014c09765453e611e2bdf20ead82813b80ff9729cb52151382e11d00e3`.
The Frappe source reference is
`4b2512ba5bf8bbc3bc12cc6beb62055c735dc629`; the runtime source reference is
`341b2df9c45b22c0579f960dcb5ecbe694cdd215`; the image-lock recording commit is
`d8bdc18b468f0e0b2507b4db3a5d0e55ef9ab2f2`. Each image label and source hash was
inspected independently.
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
