# Incident Response

**Owner:** incident commander. **Trigger:** critical alert, material SLO burn,
integrity failure, or credible security/privacy report.

1. Acknowledge, assign severity, tenant/site, commander, operations lead, and
   scribe. Preserve the alert event even during maintenance.
2. Contain: activate applicable kill switches and stop new external effects.
   Do not destroy evidence or mutate source systems.
3. Establish health/readiness, latency, error rate, saturation, queue
   depth/age/dead letters, database, backup, evidence, metric, connector, and
   audit status from governed telemetry.
4. Escalate critical incidents to the incident commander within 10 minutes and
   security/privacy owners immediately when their data is implicated.
5. Recover using a task-specific runbook. Require a second operator for
   production release or rollback.
6. Validate SLOs, integrity, reconciliation, audit continuity, and kill-switch
   state before resolving.
7. Publish a redacted timeline, decisions, impact bounds, evidence hashes, and
   follow-up owners. Do not include secret or direct-contact values.

If scope or authority is unclear, remain contained and mark the incident
`blocked_external_input`.
