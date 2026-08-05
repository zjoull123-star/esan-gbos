# ADR-0006: 版本冻结与升级

- Status: Gate 0/1 decision; runtime evidence is pending
- Date: 2026-08-06

## Context

Framework, CRM, container images, and contract versions can change
independently. Floating tags or an untested dependency update make a Gate 0
result impossible to reproduce.

## Decision

- Gate 0 inputs are the immutable commits, tags, and image digests recorded in
  [`docs/compat/versions.json`](../compat/versions.json) and summarized in the
  [compatibility matrix](../compat/compatibility-matrix.md). `latest`, moving
  branches, and unpinned transitive dependencies are not deployment inputs.
- Contract schemas use an explicit `schema_version`. A backward-compatible
  change increments the minor policy/compatibility record; a breaking change
  requires a new major schema and migration plan.
- An upgrade proposal must name an owner, reason, source reference, license
  impact, CVE/security impact, migration/rollback plan, and affected tenants.
  It must pass dependency lock checks, a disposable install, two consecutive
  migrations, contract tests, and a representative UI smoke before approval.
- The previous lock remains available for rollback until the new lock and
  migration evidence are accepted. Emergency security patches are recorded
  after containment with the same evidence requirements.
- Version drift is a blocker, not a warning to be hidden by a downgrade.

## Consequences

- Upgrades are deliberate and slower, but reproducible builds and rollback
  boundaries are explicit.
- A source release being available is not evidence that GBOS runs on it.
- Runtime evidence in the matrix remains pending until the disposable
  environment actually installs and migrates the locked applications.

## Verification gate

CI must fail on changed lock references without the corresponding compatibility
evidence. Release records link the exact commit/digest and test artifact.
