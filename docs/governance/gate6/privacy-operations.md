# Gate 6 privacy operations

This is an engineering procedure and is not legal advice. It does not determine
applicable law, lawful basis, controller or processor status, or approve a
cross-border transfer. Formal Privacy/Legal owners make and evidence those
decisions outside local fixtures.

## Role separation

- **Requester** opens a bounded request and supplies only references and
  redacted facts needed to find the record.
- **Data owner** confirms the single tenant, single site, domain, systems,
  time window and business scope.
- **Privacy reviewer** verifies identity/authority, purpose, applicable basis,
  legal hold and exceptions. The reviewer approves or denies but does not
  execute.
- **Executor** performs an approved operation and writes receipts; the executor
  cannot approve their own work.
- **Auditor** has read-only access to manifests, hashes and receipts and cannot
  request, approve or execute.

One person must never fill the requester, reviewer and executor roles for one
case. Cross-border entry requires at least two named approvers, including the
designated privacy reviewer and security owner. Role references use
role-specific prefixes, and `separation_of_duties_attested` must be true.

## Privacy request and access/export procedure

1. Register a synthetic or externally authorized request with exactly one
   `site_id` and `tenant_id`; reject any cross-site scope.
2. Verify the requester through a separately controlled process and store only
   its reference, never identity documents or contact details in the manifest.
3. Inventory the approved fields and systems. Exclude other tenants, credentials
   and unapproved raw communication content.
4. Move through `requested`, `approved`, then `executed`; never skip a state.
5. For access/export, generate a field manifest and redacted package, record
   hashes and expiry, and audit creation and download. Expired packages are
   deleted under the approved retention rule.

## Retention, deletion and consent withdrawal

Retention rules identify the data domain and class, start event, duration,
expiry action, site scope, owner approval and review date. An expiry job first
checks legal hold, dispute and approved exceptions. A missing rule or approval
fails closed.

Deletion requires a separate approval reference and a fresh legal-hold check.
An active or uncertain legal hold blocks execution. The executor covers Frappe,
Observer, evidence objects, search indexes, temporary exports, queues and the
documented backup lifecycle, then records per-system receipts and failures.
Backups expire under their independent approved schedule; no immediate purge is
claimed when it did not occur.

For consent withdrawal, stop new ingestion, model use and outbound send before
evaluating deletion. Record affected evidence references and separately approved
retention exceptions. A withdrawal does not override an active legal hold.

## Legal hold

The requester supplies a hold reference and bounded scope. The privacy reviewer
and data owner approve it; the executor snapshots evidence references and
suspends deletion and overwrite across every scoped system. Access is
least-privilege and audited. Release requires a separately authorized decision,
after which retention is recalculated; no deletion resumes from a local guess.

## Cross-border entry gate

The cross-border entry gate fails closed unless the manifest contains the
purpose, data categories, recipient/processor and subprocessors, destination
jurisdiction, externally reviewed legal mechanism/assessment, notice/consent or
other applicable basis, retention, security controls, at least two approvers,
expiry and review. Expired, missing or single-person approval blocks execution.

A local synthetic fixture only proves contract behavior. It must set
`local_fixture_is_legal_approval` to false, keep production transfer disabled,
and must not be described as legal approval. The formal Privacy/Legal review,
real personal data and Singapore cross-border approval remain
`blocked_external_input` until their actual external evidence is supplied and
independently verified.

## Audit export and evidence hygiene

Audit export contains versioned manifests, approval/evidence references,
state-transition timestamps, actor-role references, exception references,
receipts and SHA-256 integrity values. The auditor verifies hashes and scope
before release; packages are encrypted, time-limited and download-audited.

Logs, traces, manifests and evidence packages must never contain:

- secrets;
- tokens, authorization headers or session values;
- raw communication bodies;
- full phone numbers; or
- full email addresses.

Use pseudonymous subject references, masked contact fragments and controlled
object references. Detection of any prohibited value quarantines the package,
records only a safe error code and requires regeneration. Never copy the
prohibited value into the error log.
