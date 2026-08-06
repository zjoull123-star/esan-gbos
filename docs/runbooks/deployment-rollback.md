# Deployment Rollback

**Owner:** release owner. **Trigger:** failed deployment checks, critical
regression, SLO burn, integrity failure, or incident-commander decision.

1. Freeze further changes and identify immutable release and rollback targets.
2. Confirm backup freshness/integrity, migration compatibility, and whether
   schema reversal is data-safe. Use a forward fix when reversal is unsafe.
3. Require two approved records from distinct actors and roles for production
   rollback. Unresolved critical alerts remain visible.
4. Execute only through the separately authorized production release system;
   local ops helpers validate but never deploy or roll back.
5. Validate health/readiness, smoke paths, queues, both databases, audit
   continuity, metrics freshness/reconciliation, and connector kill switches.
6. Record release identities, approvals, timing, checksums, results, and
   residual risks. Re-open the incident on any failed assertion.
