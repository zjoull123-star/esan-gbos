# Gate 1 synthetic fixtures

These assets are deterministic demo inputs for Gate 0/1 validation. They are
not live operating data, a customer export, or a production seed. Identifiers,
names, contact details, and Kingdee rows are synthetic; the `example.invalid`
domain is intentionally non-routable.

The `gate1` directory contains Frappe-friendly records. Every record has a
`doctype`, stable `name`, and explicit `fields` object; fixture-only metadata
stays in the outer envelope and is stripped in `frappe_payload.json`. The GBOS
records carry `business_status` and independent `review_status`; upstream CRM
records keep their upstream status vocabulary and only use declared
`custom_esan_*` extensions for GBOS metadata.

Regenerate the checked-in JSON (stdlib only):

```sh
.venv/bin/python -m fixtures.gate1.generate
```

The generator uses seed `20260806` and the fixed demo timestamp
`2026-08-06T00:00:00Z`. A different seed is useful for local experiments but
should not be committed as a second production-like dataset.
