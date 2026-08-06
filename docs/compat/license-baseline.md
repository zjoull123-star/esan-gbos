# License baseline

| Component | SPDX | Gate 0 treatment |
|---|---|---|
| Frappe Framework | MIT | Record notices in distributed artifacts |
| ERPNext | GPL-3.0 | Preserve license and source obligations |
| Frappe CRM | AGPL-3.0 | Legal review required before any external network service |
| frappe_docker | MIT | Preserve notices for copied or adapted files |
| PostgreSQL 17 / pgvector 0.8.2 | PostgreSQL License | Preserve notices in image/SBOM; Observer use remains Gate 3 |
| `esan_gbos` custom code | AGPL-3.0-only in package metadata; license text present | Preserve source/notice obligations; external-service review remains required |

This inventory is engineering evidence, not legal advice. Because the product
may later be offered to external users over a network, the AGPL obligations and
the relationship between the custom app, Frappe CRM, and deployment artifacts
must be reviewed before Gate 5.

Gate 0 may proceed in a private repository. Package metadata and the shipped
license text now agree, but this engineering inventory is not evidence that
all notices, deployment-source-offer duties, or commercial implications have
been legally reviewed. External pilot or commercial distribution is blocked
until the review is recorded with an owner, date, and decision.
