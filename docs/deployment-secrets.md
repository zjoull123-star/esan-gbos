# Deployment secret lifecycle v1

This document is a deployment contract, not a selected platform configuration
or a production authorization.

```text
adapter_selection: blocked_platform_selection
rotation_mode: restart-bound-v1
rollback_window_minutes: 60
Production Go: false
```

Production Go stays false until the deployment platform is selected and the
rendered adapter, identities, projections, preflight evidence, recovery drill,
and production release receive separate Security, Platform, and Release Owner approvals.

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

## Eligible adapter patterns

No adapter is selected while `adapter_selection: blocked_platform_selection`.
Selection requires the operator to choose the deployment platform, demonstrate
workload-identity authentication and least privilege, and preserve the closed
delivery path above. Eligible patterns are:

- The **managed container secrets** pattern uses the container platform's managed secret
  service and workload identity, and configures the final application-visible
  target as private regular files. Environment-variable injection is invalid.
- **Kubernetes CSI or External Secrets:** retrieve by workload identity into a
  controller-only private volume, then copy into private regular files in a
  per-pod tmpfs/secret volume before application start. Native rotating or
  symlink-based projections must never be mounted directly into the application;
  copy into private regular files, apply mode 0400/0600, and mount read-only.
- **Vault Agent:** authenticate with workload identity, render to a private
  tmpfs volume as regular files with mode 0400/0600, finish before preflight,
  and give the application only the read-only mount. Dynamic in-place updates
  are not consumed in v1.

Provider-specific resource IDs, URIs, roles, policies, identities and adapter
manifests remain outside this repository template until selection and review.
The selected adapter must not widen the logical-name catalog or expose provider
payloads to the application.

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
