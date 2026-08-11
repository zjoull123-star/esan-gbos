# GBOS Tencent TKE Secret Projection Design

**Status:** Approved design only; not implemented or deployed

**Date:** 2026-08-11

**Current pilot:** Mac local pilot with the existing macOS Keychain adapter

**Future deployment target:** Tencent Cloud TKE managed cluster

**Region/account:** Deferred; no Tencent Cloud account, region, cluster, role,
secret, database, storage, network, or workload has been created by this work

## 1. Decision

The future Tencent Cloud deployment will use this closed path:

```text
Tencent Cloud SSM pinned secret version
        |
        | TKE ServiceAccount OIDC -> CAM temporary role
        v
External Secrets namespace-scoped SecretStore
        |
        | exact remoteRef.version
        v
KMS-encrypted Kubernetes Secret
        |
        | read-only source mount, projector only
        v
startup projection container
        |
        | copy into memory-backed emptyDir
        | private directory + 0400 regular files
        v
application read-only at /run/secrets
        |
        v
MountedFileSecretProvider
```

The application never calls Tencent Cloud APIs. There is no long-lived
SecretId or SecretKey in this path. The application receives no SecretId,
SecretKey, CAM token, SSM URI, Kubernetes Secret API permission, or plaintext
secret environment variable.

This design records a future platform adapter. It does not authorize an
implementation, a Tencent Cloud login, resource provisioning, a real secret
upload, or a production rollout.

## 2. Why TKE and this projection pattern

GBOS is a multi-process system with HTTP services, continuous workers, Frappe,
PostgreSQL, MariaDB, Redis, internal service names, shared POSIX evidence
storage, scheduled jobs, migrations, and ordered startup. A TKE managed cluster
can represent those boundaries without forcing every worker into a
request-driven service model.

Tencent Cloud documents a TKE ServiceAccount OIDC flow in which a Pod token is
exchanged through `AssumeRoleWithWebIdentity` for temporary credentials. The
ServiceAccount annotations bind the provider ID, role ARN, and audience. The
External Secrets Tencent provider can then read a pinned Tencent Cloud SSM
version without any long-lived SecretId or SecretKey in a Pod or Kubernetes
Secret.

External Secrets materializes a Kubernetes Secret. TKE must enable KMS-backed
envelope encryption for Secret data at rest in etcd. RBAC permits only the
External Secrets controller, the startup projector, and required control-plane
actors to access that object.

That Kubernetes Secret is a version-bound projection cache, not the source of
truth. Tencent Cloud SSM remains authoritative. The cache must be recreated
from an explicit SSM version and must never be edited manually.

Kubernetes Secret volumes use a projection implementation that is not the
application contract. GBOS deliberately rejects symlinks. Therefore the source
volume is visible only to a startup projection container. That container copies
the exact closed catalog into a memory-backed `emptyDir`, creates ordinary
files with mode `0400`, and exits successfully before the application starts.
The application mounts the destination read-only at `/run/secrets`.

## 3. Rejected alternatives

### Custom SSM client in every application

Rejected. It would couple application services to Tencent SDKs, network access,
temporary-token refresh, provider error formats, and cloud resource names. It
would also widen the runtime egress and credential surface.

### Custom shared SSM projection daemon

Deferred. It could avoid the intermediate Kubernetes Secret, but it would make
GBOS responsible for STS exchange, refresh, backoff, SSM version semantics,
audit behavior, and projector supply-chain maintenance. That complexity is not
justified before the first TKE implementation review.

### Plain Kubernetes Secret or static AK/SK

Rejected. A manually populated Secret has no authoritative SSM version chain.
A static SecretId/SecretKey creates a long-lived cloud credential and violates
the workload-identity requirement.

### Direct Secret volume in the application

Rejected. The application-visible loader requires non-symlink regular files
with mode `0400` or `0600`; native rotating/symlink projection is outside that
contract and would undermine restart-bound rotation.

## 4. Identity and authorization boundaries

The future adapter uses one namespace-scoped `SecretStore` and one dedicated
ServiceAccount/CAM role per security domain or component group. It must not use
a cluster-wide store merely for convenience.

The OIDC trust relationship must bind all of the following exactly:

- the TKE cluster OIDC provider;
- audience `sts.cloud.tencent.com`, unless an independently approved exact
  audience replaces it everywhere;
- `system:serviceaccount:<namespace>:<service-account>` subject;
- one CAM role with SSM read-only actions for the exact approved secret
  resources and versions.

The External Secrets identity may read SSM. The application ServiceAccount may
not. Neither identity may create, update, delete, list broadly, or recover SSM
secrets. No long-lived SecretId or SecretKey is created for this path.

## 5. Version and rotation contract

Every `remoteRef` uses an explicit SSM version. `latest`, implicit fallback,
`SSM_Current`, and `v_eso_latest` are not accepted as rollout intent unless the
Security and Platform owners approve a later contract revision.

V1 remains restart-bound:

1. Create a new SSM version outside the repository.
2. Create a new value-free projection declaration pinned to that version.
3. Let External Secrets create a distinct version-bound Kubernetes Secret.
4. Start a bounded replacement Pod; its projector writes a fresh memory volume.
5. Run deployment secret preflight before any database or network factory.
6. Admit the Pod only after task-specific readiness succeeds.
7. Roll the bounded workload set.
8. Keep the prior approved SSM version available for the 60-minute rollback
   window, then revoke it after explicit closure approval.

No running process consumes an in-place secret update. A failed projection,
preflight, startup, or readiness check stops the rollout and selects the prior
version through a new Pod revision.

## 6. Projector requirements

The future startup projector is a narrowly scoped, non-networked container. It
must:

- accept only the frozen logical-name and filename catalog;
- mount the source Kubernetes Secret read-only outside `/run/secrets`;
- create a memory-backed destination directory with mode `0700`;
- copy bounded bytes without logging, decoding, templating, or transforming;
- create only non-symlink regular files with mode `0400`;
- reject missing, extra, duplicate, oversized, empty, changed, or unsafe input;
- fsync completed files and the destination directory where supported;
- emit only stable value-free status codes;
- exit zero only when the entire required catalog is complete;
- have no SSM, CAM, Kubernetes API, database, connector, or provider client.

The application container depends on projector success and mounts the same
memory-backed volume read-only at `/run/secrets`. It then runs the existing
deployment preflight through `MountedFileSecretProvider` before any other
factory starts.

## 7. Platform and data topology boundaries

This document selects only the future secret-delivery pattern. It does not
select or provision:

- Tencent Cloud region, account, VPC, subnets, firewall, NAT, ingress, or DNS;
- TKE cluster size, node type, autoscaling, availability zones, or upgrade
  policy;
- TencentDB MariaDB, PostgreSQL, Redis, backups, PITR, HA, or DR;
- CFS/CFS Turbo, object storage, model artifact storage, or evidence retention;
- registry, image signing, monitoring, alert routing, or incident tooling;
- service-specific TKE Deployments, StatefulSets, Jobs, policies, or probes.

Those require a separate full deployment architecture and approval. Existing
local Compose names and local filesystem assumptions are not claimed to be
cloud-ready.

## 8. Failure and audit behavior

The adapter fails closed when OIDC, CAM trust, SSM version, External Secret
readiness, KMS encryption, source object, file mode, file type, catalog,
preflight, or task readiness is not exact. The application must not start on a
partial or stale projection.

Audit records may contain the logical name, non-secret SSM version identifier,
TKE namespace, ServiceAccount, workload revision, outcome, actor, and time.
They must not contain values, value hashes, SSM payloads, resource URIs, CAM
tokens, source volume contents, environment snapshots, usernames, or connector
credentials.

## 9. Future implementation acceptance gates

Before any Tencent Cloud pilot, a separately approved implementation must prove:

- no static AK/SK exists in the workload path;
- OIDC subject/audience/provider and CAM least privilege are exact;
- every SSM reference is version-pinned;
- TKE Secret data is KMS encrypted and RBAC is minimal;
- the application sees only 0400 regular files in a read-only mount;
- symlink, extra file, wrong mode, partial copy, version drift, OIDC failure, SSM
  denial, projector crash, and preflight failure all prevent startup;
- rotation and rollback work without exposing a value or hash;
- backups, monitoring, privacy, regional, recovery, and release approvals are
  independently complete.

Until those gates pass, `adapter_implementation=not_started`, Tencent Cloud
runtime is No-Go, and `Production Go=false`.

## 10. Official implementation references

- [Tencent Cloud TKE ExternalSecretOperator](https://cloud.tencent.com/document/product/457/132413):
  TKE ServiceAccount OIDC annotations, CAM role exchange, Tencent SSM provider,
  explicit `remoteRef.version`, refresh, and failure behavior.
- [TKE KMS encryption for etcd data](https://cloud.tencent.com/document/product/457/45594):
  KMS envelope encryption for Kubernetes Secret data in TKE managed or
  independent clusters.

These links document platform capability only. They are not evidence that a
GBOS Tencent Cloud resource exists or that the future adapter has passed its
acceptance gates.
