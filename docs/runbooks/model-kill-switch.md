# Model Kill Switch

**Owner:** AI safety owner. **Trigger:** unsafe output, prompt injection,
unexpected external action, data leakage, or audit gap.

1. Set the live-model kill switch to active and disable model-initiated
   connectors, outbound sends, and destructive capabilities.
2. Confirm requests fail closed and deterministic non-model workflows remain
   distinguishable. Do not silently substitute another model.
3. Preserve redacted prompt/output metadata, policy version, model identity if
   observed, tool calls, and audit hashes; exclude raw private content.
4. Triage policy, retrieval, connector, and authorization boundaries.
5. Re-enable only with documented remediation, safety/security approval,
   regression evidence, and two distinct production approvers.

An absent or disabled kill switch is a release-blocking critical condition.
