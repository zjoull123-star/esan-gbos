# Credential Rotation

**Owner:** security operations. **Trigger:** scheduled rotation, suspected
exposure, personnel change, or compromised connector.

1. Open a restricted change/incident record and inventory credential
   references without reading or copying their values.
2. Contain suspected compromise by disabling the capability or connector.
3. Obtain service owner and security owner approval. Production changes require
   the external secret-manager authorization for that environment.
4. Create a new version in the approved secret manager, update references,
   validate least privilege, and perform a non-secret health probe.
5. Revoke the old version only after every consumer is confirmed on the new
   version and rollback criteria are documented.
6. Verify authentication-failure and audit-write alerts. Export only redacted
   evidence and hashes.

Never transmit a credential through tickets, chat, command history, or evidence.
