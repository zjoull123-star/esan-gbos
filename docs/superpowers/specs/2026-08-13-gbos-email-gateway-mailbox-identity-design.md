# GBOS Email Gateway Mailbox Identity Design

**Status:** Approved on 2026-08-13.

**Scope:** Close the mailbox-owner identity seam in the provider-neutral Email Gateway. This
supplement does not enable provider receive, model calls, outbound send, or any real mailbox
network access.

## 1. Decision

An Integration Admin or GBOS Admin enters the canonical mailbox address once when creating or
updating a Gateway mailbox. The address is a transient command input. Frappe immediately sends
it over the authenticated local network to Observer, where the existing site- and
purpose-scoped HMAC-SHA256 identity resolver normalizes the address and derives
`extid:v1:email:<43 base64url characters>`.

Only that opaque identity reference continues into the Gateway command, PostgreSQL mailbox
record, configuration outbox, Observer configuration projection, participant authority, and
draft-finalization flow.

The existing `display_label` remains a human label such as `Gulf Sales`; it is never parsed or
treated as an address. `provider_account_ref`, `credential_ref`, and any password are likewise
not address sources.

## 2. Trust and data flow

```text
Admin browser
  canonical_mailbox_address (transient)
        |
        v
Frappe BFF -- exact admin role/team checks
        |
        | authenticated internal POST, no-store
        v
Observer mailbox identity derivation
  normalize email -> HMAC(site, observation_processing, email, normalized address)
        |
        | opaque token only
        v
Frappe idempotent command payload
        |
        v
Email Gateway mailbox + config outbox
        |
        v
MailboxConnectorProjection v2
        |
        v
Observer immutable config revision
        |
        v
Participant authority re-HMACs EML addresses and identifies mailbox_owner
```

Frappe performs derivation before creating its durable idempotency payload. Therefore the raw
address is absent from Integration Request payloads and replay receipts. Reusing an
idempotency key with a different address derives a different opaque token and is rejected as
payload drift by the existing command boundary.

## 3. Closed contracts

- The public v5 mailbox upsert operation keeps the existing operation count and adds one
  required request-only string: `canonical_mailbox_address`.
- The public response remains unchanged and never returns the raw address or opaque address
  identity.
- Observer adds one exact internal derivation request with
  `canonical_mailbox_address` and `idempotency_key`; the response contains only
  `opaque_address_ref` and `normalization_version` inside the standard no-store envelope.
- The derivation endpoint uses the existing Frappe-to-Observer mounted bearer, but a distinct
  path, authentication purpose `email_mailbox_identity`, and closed request model.
- `MailboxConnectorProjection v1.0` remains byte-for-byte compatible. New mailbox revisions use
  `MailboxConnectorProjection v2.0`, which adds exactly
  `mailbox_address_identity_ref` and includes it in `projection_digest`.
- Observer accepts both v1 and v2 projections. v1 produces a `NULL` mailbox identity and stays
  fail-closed for mailbox-owner authority; v2 persists the opaque identity on the immutable
  config revision.

## 4. Persistence and migration

- Gateway migration 009 adds nullable `mailbox_address_identity_ref` with the exact
  `^extid:v1:email:[A-Za-z0-9_-]{43}$` check.
- The column is nullable only for already-created rows. Every new BFF upsert requires a valid
  opaque token.
- Legacy rows remain readable, but `enable` and configuration relay reject a missing token.
- Updating a legacy mailbox requires re-entering its canonical address and creates a new
  config revision. There is no guessed backfill and no in-place Observer row update.
- Observer migration 016 already owns the nullable destination column. The v2 apply path writes
  it during the existing immutable insert; no applied migration is rewritten.

## 5. Runtime secret boundary

- Observer API mounts the existing `/run/secrets/identity_hmac_key` read-only and constructs
  `HmacSha256IdentityTokenResolver` through the mounted-file Secret Provider boundary.
- When Email Gateway is enabled, secret preparation materializes the existing logical
  `identity_hmac_key` even if legacy email/WeCom/WhatsApp channels are disabled.
- The key is never mounted into Gateway, Frappe, PWA, or the config relay worker.
- Missing, malformed, wrong-mode, or wrong-length identity key stops the Observer API before
  serving the derivation or participant-authority path.

## 6. UI behavior

- Gateway Admin shows a dedicated `真实邮箱地址` input with email semantics and no prefilled
  value.
- The value exists only in the input and the one request body. It is not displayed in mailbox
  cards, audit messages, URLs, local storage, service-worker caches, logs, or error text.
- The input is cleared after every completed submission, including a rejected submission.
- Editing a mailbox requires the administrator to re-enter the canonical address; the existing
  opaque token is deliberately not reversible or browser-visible.

## 7. Failure behavior

- Invalid address: safe `invalid_dto`; no address echo.
- Derivation authentication/key/service failure: safe 503; Gateway is not called.
- Gateway revision/idempotency conflict: existing bounded 409; raw address is already discarded.
- Missing legacy token: mailbox cannot be enabled; config publication is never claimed and is
  dead-lettered using a fixed safe error code.
- Projection token/digest drift: Observer rejects the revision; no partial configuration row.
- Participant HMAC mismatch or ambiguous mailbox-owner match: draft finalization remains
  fail-closed.

## 8. Acceptance criteria

1. A raw mailbox address appears in no Gateway/Observer database row, durable idempotency
   payload, projection payload after derivation, response, repr, log capture, URL, or rendered
   mailbox UI.
2. Equal canonical addresses under the same site and purpose produce the same opaque token;
   different sites produce different tokens.
3. A new mailbox revision carries the opaque identity through Gateway PostgreSQL, outbox relay,
   Observer apply, and participant-authority resolution.
4. v1/legacy rows remain readable but cannot enable or finalize mailbox-owner material.
5. Missing identity HMAC key fails before database, HTTP server, or provider construction.
6. All provider receive/send and model paths remain disabled; no real network call is part of
   this closure.
