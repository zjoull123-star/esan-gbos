# GBOS Deployment Secret Provider Design

**Status:** Approved base contract; Tencent TKE design-only addendum selected

**Date:** 2026-08-11

**Decision:** Platform-managed secrets are projected as private read-only files.
All application services consume them through one mounted-file Secret Provider.
macOS Keychain remains a local-pilot adapter only and is forbidden as a
deployment dependency.

## 1. Goals

- Keep passwords, API keys, HMAC keys, encryption keys and full connector
  credentials out of Git, images, environment variables, command arguments,
  logs, Frappe site config and evidence bundles.
- Keep application code independent of Keychain, Vault and cloud-vendor APIs.
- Preserve the existing /run/secrets/<logical-name> runtime boundary.
- Support text, opaque bytes and closed JSON credentials without lossy encoding.
- Make missing, malformed, over-permissioned or stale secret mounts fail before
  database, provider or connector access.
- Support audited version rotation through a bounded deployment restart.

The future provider choice is recorded in
[GBOS Tencent TKE Secret Projection Design](2026-08-11-gbos-tencent-tke-secret-projection-design.md).
No adapter or cloud resource is implemented by that selection.

## 2. Non-goals

- Provisioning or implementing the selected future Tencent TKE adapter in this change.
- Allowing applications to fetch secrets over the network.
- Hot-reloading secrets inside long-running workers in v1.
- Storing secret hashes in logs or evidence. Low-entropy secrets must not become
  offline guessing oracles.
- Treating local Keychain availability as production readiness.

## 3. Architecture

    Platform secret store
            |
            | authenticated platform projection
            v
    private tmpfs / secret volume
            |
            | regular file, 0400/0600, read-only in app container
            v
    MountedFileSecretProvider
            |
            +--> SecretBytes
            +--> SecretText
            +--> closed JSON bytes
            |
            v
    domain-specific validators and service composition

There are two separate trust adapters:

1. Local adapter: macOS Keychain is read by the host-only local-pilot
   materializer. It writes repository-external 0600 files and deletes them on
   stop or failure.
2. Deployment adapter: the selected deployment platform retrieves versioned
   values from its managed secret store and projects them into a private tmpfs
   or secret volume. The application container receives only read-only regular
   files.

Neither adapter is imported by application services. Both must produce the same
mounted-file contract.

## 4. Secret Provider contract

MountedFileSecretProvider owns file-system safety and bounded reading:

- root is an absolute configured directory, normally /run/secrets;
- callers use a closed logical name, never an arbitrary path;
- names map to a fixed allow-listed filename;
- lstat and open(O_NOFOLLOW | O_CLOEXEC) must identify the same inode;
- the target must be a regular non-symlink file with mode 0400 or 0600;
- reads are bounded and detect short reads, growth, replacement and early EOF;
- text secrets allow one terminal LF and reject empty, NUL, CR or embedded LF;
- byte secrets preserve exact bytes and enforce exact/minimum/maximum sizes;
- JSON secrets are returned as bytes; the owning domain performs duplicate-key,
  field-set and semantic validation;
- returned wrappers redact repr, str and exceptions.

The provider must not enumerate arbitrary files, render secret names in public
health output, or expose a generic reveal-all operation.

## 5. Deployment projection contract

The deployment control document contains metadata only:

- schema and site identifier;
- deployment environment;
- logical secret name;
- expected target filename;
- value kind: text, bytes or closed_json;
- size boundary;
- non-secret platform version identifier;
- owning component and required or optional state.

It never contains a secret value, Keychain URI, cloud secret payload, reversible
encoding or secret digest.

The platform adapter must:

1. authenticate using the workload identity supplied by the platform;
2. fetch only the allow-listed secret versions;
3. project each value to a private in-memory volume;
4. set ownership to the exact runtime UID/GID and mode 0400 or 0600;
5. bind the resulting files read-only into application containers;
6. run the Secret Provider preflight before the application starts.

Kubernetes-style symlink projections must not be mounted directly because
current runtime loaders intentionally reject symlinks. A platform init or
sidecar copies the selected version into an application-private emptyDir or
tmpfs as a regular file, then the application mounts that destination
read-only.

## 6. Rotation and recovery

V1 uses restart-bound rotation:

1. create a new platform secret version;
2. render it into a new private projection directory;
3. run preflight without starting application traffic;
4. roll the affected component;
5. verify health and provider or connector authentication;
6. revoke the old version after the bounded rollback window.

Rollback selects the previous platform version and repeats projection and
preflight. No process reads a changing file in place. API keys that require
overlap keep old and new versions valid only for the approved rollback window.

## 7. Failure behavior and observability

- Missing, unsafe, oversized, malformed or unapproved-version files exit with
  the existing fail-closed configuration status before DB or network access.
- Logs contain only stable error codes and component names, never values, paths,
  usernames, Keychain references, hashes or provider payloads.
- Metrics are low-cardinality counts for preflight success/failure and rotation
  age. They do not label by account, secret name or version.
- The deployment audit records platform version identifiers, actor or workload,
  approval, rollout time and rollback result. It does not record values.

## 8. Platform implementation gate

The application contract is implemented independently of a platform. The
future design-only selection is Tencent Cloud TKE ServiceAccount OIDC + SSM +
External Secrets + KMS-encrypted Kubernetes Secret + private regular-file
projection into a memory-backed `emptyDir`. The application mounts only the
destination read-only.

The platform adapter remains unimplemented and is not production-ready until
the separately approved Tencent TKE design acceptance gates pass.

Plain Docker Compose env_file, plaintext bind-mounted host files, SOPS plaintext
at runtime and secrets baked into images are not approved deployment sources.

## 9. Acceptance criteria

- All application secret reads go through MountedFileSecretProvider or a domain
  validator receiving its bounded bytes.
- A deployment-mode preflight rejects Keychain references and plaintext secret
  environment variables.
- Text, 32-byte binary and full Email JSON credentials round-trip without
  truncation or encoding drift.
- Symlink, wrong mode, inode swap, short read, early EOF, oversized file and
  additional JSON fields are covered by RED-to-GREEN tests.
- Local-pilot Keychain behavior remains available only behind the local adapter.
- No real secret appears in fixtures, command lines, logs, repository files or
  evidence artifacts.
