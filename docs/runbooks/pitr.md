# Point-in-Time Recovery

**Owner:** recovery owner. **Trigger:** logical corruption or recovery-point
test requiring a point newer than the last full backup.

1. Define tenant/site, database, target UTC instant, incident/change record,
   RPO/RTO targets, and approved isolated destination.
2. Verify base backup checksum and continuity/integrity of MariaDB binlogs or
   PostgreSQL WAL through the target instant.
3. Restore base backup into a new isolated target and replay only through the
   target instant. Never replay into or delete the source.
4. Verify transaction boundary, schema, counts, tenant isolation, audit chain,
   and application invariants against predeclared assertions.
5. Record observed data gap and recovery duration; fail on any RPO, RTO, or
   integrity breach.
6. Production cutover requires recovery, security, and two-person release
   authorization through the external platform process.

If log continuity is uncertain, stop and choose an earlier verified point.
