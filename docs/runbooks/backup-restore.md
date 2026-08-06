# Backup Restore

**Owner:** recovery owner. **Targets:** MariaDB, PostgreSQL/pgvector, object
storage, configuration, and required evidence metadata.

1. Select an immutable backup by tenant/site, database, creation time, source,
   retention class, encryption identity, and SHA-256 checksum.
2. Assert backup age is within the declared RPO and every required component is
   present. A missing, stale, or checksum-mismatched backup fails the drill.
3. Restore only into a new isolated target. Never overwrite or delete the
   source, current target, volume, or backup.
4. Apply pinned compatible migrations, then verify schema/version, row counts,
   tenant isolation, application invariants, object references, audit
   continuity, and evidence checksums.
5. Measure from authorization to verified service as observed RTO; measure data
   gap as observed RPO. Both must be less than or equal to their targets.
6. Keep production traffic disconnected until recovery and security owners
   approve. Production execution requires external approval artifacts and the
   platform-specific procedure.

The local `scripts/ops/gate6_ops.py` checker reads synthetic files only and
executes no database command.
