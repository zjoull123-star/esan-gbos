# Email Gateway Mailbox Identity Implementation Plan

> **2026-08-27 successor status:** This plan's 19-operation baseline is historical. The current v5
> OpenAPI has 21 operations after SLA extensions, while the PWA still omits the two specialized
> send-review operations. Current progress and production-test gates are maintained in
> [HANDOFF](../../HANDOFF.md).

> **Execution:** Use strict RED -> GREEN TDD in the existing isolated worktree. Preserve the
> frozen v1 projection and all unrelated work. Do not use provider credentials, provider
> network, model network, real external send, or production mutation.

**Goal:** Let an administrator enter a canonical mailbox address once while persisting and
projecting only a site/purpose-scoped opaque HMAC identity, so Observer can safely resolve the
mailbox-owner participant.

**Architecture:** Frappe sends the transient address to an authenticated Observer derivation
endpoint before durable idempotency. Gateway stores the returned opaque reference, publishes it
through a new v2 connector projection, and Observer writes it into the existing immutable config
revision. Observer API also loads the same identity HMAC key for final MIME participant
revalidation.

**Stack:** Python 3.14, Frappe v16 BFF, FastAPI, PostgreSQL/RLS, Vue 3/TypeScript, JSON Schema,
Docker Compose static composition.

---

## Task 1: Freeze transient input and projection v2 contracts

**Files:**

- Create: `contracts/email_gateway/mailbox-connector-projection-v2.0.schema.json`
- Modify: `contracts/bff-v5.openapi.json`
- Modify: `contracts/README.md`
- Modify: `tests/contracts/test_email_gateway_contracts.py`
- Modify: `tests/contracts/test_bff_v5_openapi.py`

**RED:** Add tests proving the v5 surface remains exactly 19 operations, mailbox upsert requires
`canonical_mailbox_address`, no mailbox response exposes it, v1 remains unchanged, and v2 is the
v1 field set plus exact `mailbox_address_identity_ref` covered by `projection_digest`.

Run:

```bash
uv run --frozen pytest -q tests/contracts/test_email_gateway_contracts.py tests/contracts/test_bff_v5_openapi.py
```

Expected: failures for the absent request field and v2 schema.

**GREEN:** Add the closed request-only field and v2 schema. Do not add any public operation or
response field.

## Task 2: Persist only the opaque mailbox identity in Gateway

**Files:**

- Create: `services/email_gateway/migrations/009_email_gateway_mailbox_identity.sql`
- Modify: `services/email_gateway/models.py`
- Modify: `services/email_gateway/api.py`
- Modify: `services/email_gateway/repositories/mailboxes.py`
- Modify: `tests/email_gateway/test_models.py`
- Modify: `tests/email_gateway/test_api.py`
- Modify: `tests/email_gateway/test_postgres_repositories.py`
- Modify: `tests/email_gateway/test_migrations.py`
- Modify: `tests/integration/test_email_gateway_postgres.py`

**RED:** Cover strict opaque-token validation, new-upsert requirement, public response omission,
legacy nullable read, legacy enable rejection, revision/idempotency binding, v2 claim payload,
RLS/grants, migration double-run, and full durable round-trip. Include sentinel raw addresses and
assert they never appear in SQL parameters, receipts, repr, or projection wire.

**GREEN:** Add the nullable migration column, optional legacy model field, required internal BFF
upsert field, status guard, repository persistence, claim propagation, and v2 projection. Never
encrypt or persist the raw canonical address; `address_display_ciphertext` remains only the
human display label.

## Task 3: Derive and apply mailbox identity in Observer

**Files:**

- Create: `services/observer/observer/email_mailbox_identity.py`
- Modify: `services/observer/observer/email_connector_config.py`
- Modify: `services/observer/observer/local_pilot_api.py`
- Modify: `services/observer/observer/runtime.py`
- Modify: `services/local_pilot_runtime/observer_api.py`
- Modify: `tests/observer/test_email_mailbox_identity.py`
- Modify: `tests/observer/test_email_connector_config.py`
- Modify: `tests/observer/test_local_pilot_api.py`
- Modify: `tests/local_pilot_runtime/test_observer_api.py`

**RED:** Prove normalization/case-fold stability, cross-site separation, safe repr/errors, closed
derive request/response, exact purpose/authentication, v1 nullable compatibility, v2 insert and
replay, digest/token drift rejection, and production participant resolver receiving the same
HMAC resolver. Prove missing or malformed key fails before connection/server factories.

**GREEN:** Add a stateless derivation service using `HmacSha256IdentityTokenResolver`, the exact
internal endpoint, dual v1/v2 config parsing, immutable v2 insert, and runtime resolver injection.
Use `observation_processing` as the HMAC purpose while requiring the HTTP purpose
`email_mailbox_identity`.

## Task 4: Keep raw input out of Frappe durability

**Files:**

- Modify: `apps/esan_gbos/esan_gbos/api/v5/gateway.py`
- Modify: `apps/esan_gbos/esan_gbos/api/v5/email_admin.py`
- Modify: `apps/esan_gbos/esan_gbos/domain/v5_email_dto.py`
- Modify: `tests/domain/test_bff_v5_email.py`
- Modify: `tests/domain/test_bff_v5_runtime.py`

**RED:** Assert Observer derivation happens before Gateway call, Gateway receives only
`mailbox_address_identity_ref`, the `run_idempotent` payload excludes the raw address, Observer
failure prevents Gateway calls, response/log/error capture omits raw addresses, and replay/drift
behavior remains closed.

**GREEN:** Add the transient public parameter, derive it through the existing mounted
Frappe-to-Observer client with the distinct path/purpose, validate only the opaque result, and
then build the durable command. Never pass the raw field to `validate_mailbox_upsert`,
`scope_payload`, `_mailbox_command`, or `run_idempotent`.

## Task 5: Wire the existing HMAC secret into Observer API composition

**Files:**

- Modify: `scripts/local-pilot/prepare-secrets`
- Modify: `infra/local/compose.yml`
- Modify: `infra/local/runtime-entrypoints.json`
- Modify: `tests/infra/test_local_pilot_scripts.py`
- Modify: `tests/infra/test_email_gateway_runtime_composition.py`

**RED:** With Email Gateway enabled and legacy channels disabled, require exactly one
`identity_hmac_key` materialization; require only observer-api and existing connector identities
to mount it; reject missing/wrong-size key before database/server factories. Assert Frappe and
Gateway services never mount the key.

**GREEN:** Extend the existing conditional materialization and observer-api read-only secret
mount. Do not add a new secret value or expose the key through runtime JSON.

## Task 6: Add the transient admin UI

**Files:**

- Modify: `apps/esan_gbos/frontend/src/api/email-gateway-types.ts`
- Modify: `apps/esan_gbos/frontend/src/api/email-gateway.ts`
- Modify: `apps/esan_gbos/frontend/src/views/EmailGatewayAdminView.vue`
- Modify: `apps/esan_gbos/frontend/tests/email-gateway.test.ts`
- Modify: `apps/esan_gbos/frontend/tests/v5.test.ts`
- Modify: `apps/esan_gbos/frontend/e2e/gbos.spec.ts`

**RED:** Require a dedicated email input, request-only command field, clearing after both success
and rejection, no raw address in cards/audit/URL/local storage/rendered errors, 409 reload,
keyboard/axe, 375/768/1440, and true 200% zoom.

**GREEN:** Add the transient field and clear it after every completed request. Do not prefill it
when editing and do not add it to mailbox response parsing.

## Task 7: Prove the offline chain and run the release matrix

**Files:**

- Modify: `tests/integration/test_email_gateway_offline_e2e.py`
- Modify: `docs/HANDOFF.md`
- Modify: `docs/local-pilot/IMPLEMENTATION_PLAN.md`

**RED/GREEN chain:** Extend the real offline component chain from admin upsert through Observer
HMAC derivation, Gateway migration/repository/config outbox, relay, Observer config apply,
publication/CAS, and participant-authority finalization. Assert one mailbox-owner match succeeds;
wrong site, wrong token, missing token, and revision drift fail closed. Scan all durable/public
artifacts for the sentinel raw address.

Run focused and full gates serially:

```bash
uv run --frozen pytest -q tests/contracts tests/domain tests/email_gateway tests/observer tests/local_pilot_runtime tests/action_guard tests/infra
uv run --frozen pytest -q tests/integration/test_email_gateway_offline_e2e.py tests/integration/test_email_gateway_human_operations_offline_e2e.py tests/integration/test_email_send_command_offline_e2e.py
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy services/email_gateway services/observer/observer services/local_pilot_runtime apps/esan_gbos/esan_gbos/domain
uv run --frozen python -m compileall -q apps services tests scripts
scripts/dev/test-email-gateway-postgres --all
scripts/dev/test-email-gateway-frappe
corepack pnpm --dir apps/esan_gbos/frontend run lint
corepack pnpm --dir apps/esan_gbos/frontend run test:unit
corepack pnpm --dir apps/esan_gbos/frontend run typecheck
corepack pnpm --dir apps/esan_gbos/frontend run build
corepack pnpm --dir apps/esan_gbos/frontend run test:e2e
git diff --check
```

Update handoff truth only from fresh results. State explicitly that provider receive/send and
model network remain disabled and that official WeCom outbound reconciliation evidence is still
absent.
