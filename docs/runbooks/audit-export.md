# Audit Export

**Owner:** security operations. **Trigger:** approved audit, incident,
regulatory, privacy, or tenant evidence request.

1. Validate requester, tenant, purpose, legal basis, time range, fields, and
   destination. Cross-border export requires its separate approval.
2. Query immutable audit sources with bounded filters; never accept arbitrary
   SQL or URLs.
3. Remove secrets, tokens, raw communication content, complete phone numbers,
   and complete email addresses unless a restricted legal decision explicitly
   requires them.
4. Produce a manifest with source ranges, row counts, redaction policy, creation
   time, exporter identity, and SHA-256 checksums.
5. A second reviewer confirms scope, redaction, integrity, and destination
   before external delivery.
6. Record export access, delivery receipt, retention, and disposal deadline.

An audit-write failure is critical and blocks release.
