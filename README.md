# ESAN GBOS

ESAN Global Business Operating System is an AI-native front-office platform for
global sales, product sampling, sourcing collaboration, communication
observation, and governed business insight.

## Delivery boundary

The current delivery is limited to Gate 0 and Gate 1:

- architecture, governance, contracts, version compatibility, and CI;
- a local Frappe v16 / ERPNext v16 / Frappe CRM v1 foundation;
- the `esan_gbos` custom application and Chinese responsive PWA;
- fixture-backed sales, sampling, sourcing, work-item, and review workflows.

Kingdee remains the system of record for orders, inventory, and finance. This
repository does not provide Kingdee write capabilities in V1.

## Safety defaults

- Production channel ingestion is disabled.
- Real AI model calls are disabled.
- Kingdee integration is mock-only during Gate 0 and Gate 1.
- Secrets and raw business exports must never be committed.

Implementation and verification instructions will be added on the Gate 0/1
feature branch after the compatibility baseline is frozen.
