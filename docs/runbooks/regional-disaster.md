# Regional Disaster Recovery

**Owner:** incident commander and recovery owner. **Trigger:** declared region
loss or an approved regional DR exercise.

1. Declare the event, freeze releases, activate connector/outbound/model kill
   switches, and confirm the affected tenant and control-plane status.
2. Select a preapproved recovery region and verify legal, privacy,
   cross-border, network, identity, capacity, and encryption entry gates.
3. Verify replicated backup age, checksum, object inventory, and database log
   continuity before creating isolated recovery targets.
4. Restore MariaDB, PostgreSQL/pgvector, object data, configuration, and audit
   evidence using the backup/PITR runbooks. Do not overwrite the failed region.
5. Validate health/readiness, queues, tenant isolation, metrics reconciliation,
   evidence/audit integrity, and fail-closed connector/model state.
6. Measure observed RPO/RTO against declared targets. Any breach keeps traffic
   disabled and escalates to the incident commander.
7. Production traffic change requires two-person authorization and external
   platform approvals. Plan failback as a separate controlled change.
