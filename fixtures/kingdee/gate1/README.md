# Kingdee Gate 1 mock

`mock.py` is a local, controlled read-only adapter. Its allowlist contains only
`query_metadata`, `query_bill`, `query_bill_json`, `count_bill`,
`query_bill_all`, `query_bill_to_file`, `query_bill_range`, and `view_bill`.
There are no writer methods and no network or credential access. Requests must
carry the full bounded shape declared by `REQUEST_FIELDS`; responses include a
fixed synthetic source and page envelope.

Example:

```python
from fixtures.kingdee.gate1.mock import KingdeeMock

mock = KingdeeMock()
response = mock.query_bill(mock.default_request(form_id="BD_MATERIAL"))
```

All rows and identifiers are demo-only (`KD-SYNTH-*`). The query time is the
fixed `2026-08-06T00:00:00Z`; it is not evidence of live Kingdee availability.
