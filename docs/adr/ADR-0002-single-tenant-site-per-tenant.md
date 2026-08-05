# ADR-0002: 单租户首发与 site-per-tenant

- Status: Gate 0/1 decision; runtime evidence is pending
- Date: 2026-08-06

## Context

The first release needs a small, auditable blast radius. Shared-site tenancy
would make database queries, files, queues, exports, and operator access
dependent on every caller remembering a tenant filter.

## Decision

- The initial pilot is **one tenant at a time**. No multi-tenant production
  launch is implied by Gate 0/1 fixtures.
- Each tenant gets a separate Frappe site (`site_id`) with its own database,
  files, queue namespace, backups, and configuration boundary. A deployment
  may host multiple sites later, but they remain independently addressable.
- Observer storage and checkpoints use the same `site_id` partition and must
  not be queried across sites. Object-store prefixes and encryption/key
  boundaries are tenant-scoped.
- Administrative access is tenant-scoped. A cross-site operation requires a
  documented break-glass ticket, time limit, least privilege, and an audit
  record; it is not a normal application role.
- Fixtures may contain more than one site only to test isolation. They do not
  represent a permitted shared-site production topology.

## Consequences

- Site creation, migration, backup/restore, export, and deletion are repeated
  per tenant and need explicit runbooks.
- Capacity and cost are higher than a shared site, while isolation failures
  are easier to detect and contain.
- A future shared-site design requires a new ADR, tenant-isolation tests, and
  an explicit approval; it is not an optimization hidden inside V1.

## Verification gate

Gate 1 tests must attempt cross-site reads, writes, file access, queue jobs,
and exports and expect denial. Evidence must include the site identifier and
the test revision; a green unit test alone is not a production isolation claim.
