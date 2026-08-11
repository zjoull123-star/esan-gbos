# Deployment secret lifecycle v1

This document is a deployment contract, not a selected platform configuration
or a production authorization.

```text
adapter_selection: planned_tencent_tke_oidc_ssm_external_secrets
adapter_implementation: not_started
current_pilot: macos_local_keychain
future_runtime: tencent_tke_managed_cluster
region_selection: deferred
rotation_mode: restart-bound-v1
rollback_window_minutes: 60
Production Go: false
```

The future platform pattern is selected for design purposes only. Production Go
stays false until a Tencent Cloud region and account are approved and the TKE
adapter, identities, projections, preflight evidence, recovery drill, and
production release receive separate Security, Platform, and Release Owner approvals.

## Closed delivery path

macOS Keychain is local-only. It may materialize local-development secrets into
private files, but it is not a deployment secret store and no deployed workload
may invoke Keychain or the macOS `security` tool.

Every deployed secret follows one path:

1. The selected platform stores secret material under platform-managed version identifiers.
2. A least-privilege workload identity requests only its approved versions.
3. The platform performs authenticated projection into a private tmpfs or secret volume.
4. The adapter exposes regular files with mode 0400 or 0600; symlink projection is forbidden.
5. The workload receives a read-only application mount at `/run/secrets`.
6. Application code reads only through `MountedFileSecretProvider` and its closed catalog.

The projection metadata is
[`infra/prod/secret-provider-v1.template.json`](../infra/prod/secret-provider-v1.template.json).
It repeats the authoritative Gate 6 catalog, contains placeholder non-secret
version IDs, and contains no vendor resource identifier or secret material.

The following boundaries are unconditional:

- No plaintext secret in environment variables.
- No plaintext secret in process arguments.
- No plaintext secret in repository files or Git history.
- No plaintext secret in container image layers or build arguments.
- No plaintext secret in logs, traces, metrics, errors, or evidence bundles.
- No plaintext secret in Frappe site config, including `site_config.json` and `common_site_config.json`.

Audit metadata contains no secret values or secret hashes. An audit event may
record the logical name, non-secret platform version ID, workload identity,
site/environment, outcome status, actor, and timestamp. It must not record a
provider payload, resource URI, vendor resource ID, file contents, command line,
or environment snapshot.

## Selected future adapter

The approved design-only target is Tencent Cloud TKE:

1. A namespace-scoped External Secrets `SecretStore` uses TKE ServiceAccount
   OIDC and a least-privilege CAM temporary role. No long-lived SecretId or
   SecretKey is stored in the workload path.
2. External Secrets reads only explicit Tencent Cloud SSM versions and writes a
   version-bound Kubernetes Secret.
3. TKE KMS envelope encryption protects Kubernetes Secret data in etcd, with
   minimal RBAC.
4. A non-networked startup projector reads the source volume, copies the closed
   catalog into a memory-backed `emptyDir`, and creates private regular files
   with mode 0400.
5. The application sees only that destination, mounted read-only at
   `/run/secrets`, and runs the stable preflight before any DB or network access.

Direct Kubernetes symlink projection, plaintext environment variables, static
AK/SK, broad cluster-scoped stores, and application-side Tencent SDK calls are
invalid. The detailed design is
[GBOS Tencent TKE Secret Projection Design](superpowers/specs/2026-08-11-gbos-tencent-tke-secret-projection-design.md).

This selection does not implement an adapter. Provider resource IDs, URIs,
roles, policies, manifests, regions, accounts and secret versions remain
external and unselected. The adapter must not widen the logical-name catalog or
expose provider payloads to the application.

## Rotation and rollback procedure

V1 rotation is restart-bound: a running process never consumes an in-place
change. The stable preflight must pass before rollout begins, using only stable
status codes and no payload-bearing output.

1. **Create a new platform secret version** and record only its non-secret version ID.
2. **Project the candidate into the private volume** using the workload identity; copy to fresh regular files with the required mode.
3. **Run the stable preflight** against every required logical name before any database, connector, provider, Frappe, or application startup.
4. **Restart a bounded canary** so it opens only the candidate files through `MountedFileSecretProvider`.
5. **Prove health** with task-specific readiness, authentication, and dependency evidence that contains neither values nor hashes.
6. **Complete the bounded rollout** in controlled batches, re-running preflight before each restarted batch and stopping on any stable failure code.
7. **Revoke the old version** only after the rollout, health proof, rollback window, and approvals are complete.

Keep the previous approved version accessible only to the authorized projection
identity for `rollback_window_minutes: 60`. During that bounded window, a failed
canary or rollout must stop further restarts, rollback to the previous approved version,
re-project it into new private regular files, run preflight, and restart the affected workloads.
Prove health again. After 60 minutes of healthy service
and explicit closure approval, revoke the previous version and retain only
value-free audit metadata.

Emergency rollback does not permit an environment variable, command argument,
repository file, image layer, log entry, Frappe site config value, local
Keychain lookup, direct symlink mount, or skipped preflight.
