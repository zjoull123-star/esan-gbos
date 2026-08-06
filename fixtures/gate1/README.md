# Gate 1 business fixture set

The generated records cover synthetic CRM Organization/Contact/Lead/Deal
links and a representative forward-owned closure:

`CRM Deal -> GBOS Product Brief -> GBOS Sample Project -> GBOS Sample Iteration -> GBOS Sample Shipment -> GBOS Sample Feedback -> GBOS Demand Signal -> GBOS Sourcing Event (candidates) -> GBOS Work Item -> GBOS Review Case`

The GBOS side owns the forward links. A CRM Deal is linked by the Product
Brief's `deal` field; GBOS extensions on CRM records are prefixed
`custom_esan_` and remain separate from upstream CRM status fields. The Demand
Signal records the Feedback hand-off in its deterministic `origin_reference`
while retaining only fields declared by the GBOS DocType.

`records.json` is the complete ordered set. Per-doctype files are convenient
for Frappe fixture loaders. `frappe_payload.json` is the import view: it strips
the synthetic envelope and embeds `GBOS Sourcing Candidate` rows under their
parent event. `manifest.json` records the fixed seed and counts, while
`status_allowlist.json` makes the planned state vocabulary auditable.
