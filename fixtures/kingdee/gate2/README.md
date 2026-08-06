# Gate 2 Kingdee read-projection mock

This package freezes a deterministic, synthetic read-projection boundary for
exactly seven logical objects: material, customer, supplier, sales order,
purchase order, inventory, and receivable.

Gate 2 has no Kingdee connection, credential, metadata lookup, HTTP client,
socket, subprocess, generic form/field/filter/order forwarding, SQL, DocType
proxy, or writer. The candidate forms and fields in the dictionary are design
hypotheses marked `gate5_metadata_required`; they are not verified metadata.

The public mock tools are:

- `kingdee.material.get`
- `kingdee.customer.get`
- `kingdee.supplier.get`
- `kingdee.sales_order.get`
- `kingdee.purchase_order.get`
- `kingdee.inventory.get`
- `kingdee.receivable.get`

Each response carries the explicit site, synthetic account-set reference,
dictionary and allow-list versions, Crosswalk/evidence status, fixed query
time, and measured zero-network/zero-credential/zero-subprocess controls.
