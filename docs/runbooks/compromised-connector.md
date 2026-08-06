# Compromised Connector

**Owner:** integration security. **Trigger:** unexpected connector state,
destination-policy violation, token exposure, or unauthorized external effect.

1. Activate the connector kill switch and outbound-send kill switch; preserve
   audit and network-policy evidence.
2. Revoke connector credentials using the credential-rotation runbook and
   block the affected destination identity.
3. Bound tenant, account-set, operation, time, rows, and external effects.
4. Verify that Kingdee and other source systems were not mutated. Escalate any
   mutation indication as a critical security incident.
5. Reconcile immutable audit events with connector requests and responses.
6. Re-enable only after security approval, destination allowlist validation,
   a read-only canary, and two-person production authorization.

Live failure must remain unavailable; never fall back to synthetic results.
