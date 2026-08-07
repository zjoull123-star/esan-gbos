# Local pilot composition

This directory declares an isolated, default-off local topology. Its current
composition state is `not_composed`: blocked service entrypoints and unrecorded
local image identities prevent a real start. The checked-in manifest also keeps
`local_pilot_go=false`. `scripts/local-pilot/start` therefore fails before
secret materialization or container actions.

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

The Frappe, MariaDB, Redis, backend, worker, scheduler, websocket, and PWA
services use local-pilot-specific volume names. The Frappe services require the
unbuilt local image `esan-gbos-local-pilot-frappe:2026-08-08`; the upstream
ERPNext image is not treated as a GBOS PWA. Site setup checks and installs
`erpnext`, `crm`, and `esan_gbos`, then runs `bench migrate` twice. The optional
`frappe-synthetic-bootstrap` profile reads a repository-external test-user
password from `/run/secrets/frappe_demo_password` and calls only the governed
synthetic fixture seed. After the approved stack is running, the explicit path
is `scripts/local-pilot/bootstrap-synthetic-user --acknowledge-synthetic`.
The helper reruns the real go/image/composition preflight and refuses to start
missing dependencies implicitly. It is declared but has not been executed.
`/gbos` remains unverified until that image is built and the route probe passes.

Published PostgreSQL, MariaDB, API, PWA, webhook, and optional monitoring ports
bind only to `127.0.0.1`. Cloudflare Tunnel is profile-gated, has no API network
membership, and routes only to WhatsApp webhook ingress.

`scripts/local-pilot/preflight --synthetic` validates only the checked-in,
fully disabled smoke configuration. It is mutually exclusive with
`--require-go` and does not waive the real composition, image, or governance
gates.

Build the runtime image explicitly with
`scripts/local-pilot/build-runtime-image`. The command disables pulling and
atomically records the inspected local image ID only after a successful build.
Build the GBOS Frappe/PWA image separately with
`scripts/local-pilot/build-frappe-image --confirm-network-build`; the explicit
flag acknowledges that the governed upstream builder verifies remote tags.
Neither command has been run by this composition task, and neither lock entry
is invented.
