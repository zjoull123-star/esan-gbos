# Support Access

**Owner:** support security owner. **Trigger:** approved tenant support case
requiring elevated or data-bearing access.

1. Verify case, tenant, purpose, data classification, requested scope, expiry,
   and customer/privacy approval where required.
2. Grant a named, least-privilege, time-bounded role; shared accounts and
   standing elevation are prohibited.
3. Require MFA and record access start/end, objects accessed, and changes in
   immutable audit events.
4. Mask secrets, tokens, raw communications, complete phone numbers, and
   complete email addresses in screenshots and exports.
5. Revoke access at expiry or case closure and verify revocation.
6. Have a second reviewer reconcile the case, grant, access logs, and revocation.

Emergency access requires an active incident and retrospective review.
