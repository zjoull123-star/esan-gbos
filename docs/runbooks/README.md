# Gate 6 Operations Runbooks

These runbooks are decision procedures, not authorization to operate a live
environment. Production execution requires the named incident/change record,
two distinct authorized people where stated, and the external approvals for
the affected tenant, region, and data class.

Common rules:

1. Start an immutable incident or change record; use UTC timestamps.
2. Do not place secrets, tokens, raw communications, complete phone numbers,
   or complete email addresses in logs or evidence.
3. Prefer containment and reversible controls. Never delete data during
   diagnosis.
4. Record commands by reference and store redacted output with checksums.
5. Critical alerts are never suppressed by maintenance windows. Noncritical
   paging may be suppressed only for an approved window of at most 120 minutes;
   alert events and SLO accounting continue.
6. Production release and rollback require two distinct approved actors in
   different roles. Local tools only validate artifacts; they do not execute.

Runbooks cover incident response, credentials, connectors, models, breaches,
privacy, support, audit export, rollback, backup restore, PITR, and regional
disaster recovery.
