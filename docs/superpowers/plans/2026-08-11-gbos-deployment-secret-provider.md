# GBOS Deployment Secret Provider Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development
> if subagents are available or superpowers:executing-plans otherwise. Steps use
> checkbox syntax for tracking.

**Goal:** Make every deployed GBOS service consume platform-managed secrets from
one strict read-only mounted-file provider while keeping macOS Keychain local-only.

**Architecture:** The platform retrieves versioned secrets using workload
identity and projects them into a private volume. Application code uses
MountedFileSecretProvider for bounded text, byte and closed-JSON reads. Domain
validators remain responsible for semantic validation. Rotation is versioned
and restart-bound in v1.

**Tech Stack:** Python 3.14, JSON Schema 2020-12, Docker or Kubernetes secret
volumes, pytest, Ruff, mypy.

**Execution status (2026-08-11):** Tasks 1–6 are implemented and verified in
the current feature lineage. Task 7 has only its design-selection step
completed. The future Tencent TKE adapter implementation remains unauthorized
and not started.

---

## Chunk 1: Application Secret Provider

### Task 1: Add strict mounted-file primitives

**Files:**

- Create: services/local_pilot_runtime/secret_provider.py
- Create: tests/local_pilot_runtime/test_secret_provider.py

- [ ] **Step 1: Write failing tests for the closed logical-name allow-list**

  Cover an approved logical name and rejection of traversal, slash, unknown
  name, absolute path, NUL and control characters.

- [ ] **Step 2: Run the focused test and verify RED**

  Run:

      uv run --frozen pytest tests/local_pilot_runtime/test_secret_provider.py -q

  Expected: collection failure because secret_provider does not exist.

- [ ] **Step 3: Add redacted value wrappers and the provider interface**

  The public surface is:

      SecretSpec(name, filename, kind, minimum_bytes, maximum_bytes,
                 exact_bytes=None, required=True)
      MountedFileSecretProvider(root, specs)
      provider.read_bytes(name)
      provider.read_text(name)
      provider.read_json_bytes(name)

  Secret values must not appear in repr, str, exceptions or equality failures.

- [ ] **Step 4: Add failing file-safety tests**

  Cover mode 0400 and 0600 success; symlink, directory, wrong mode, short read,
  early EOF, inode replacement, growth and maximum size rejection.

- [ ] **Step 5: Implement bounded O_NOFOLLOW reads**

  Use lstat, os.open, fstat, complete bounded reads and post-read inode/size
  comparison. Never use Path.read_bytes for secret payloads.

- [ ] **Step 6: Add text and binary behavior tests**

  Text permits one terminal LF and rejects empty, NUL, CR or embedded LF.
  Binary preserves all 256 byte values and supports exact 32-byte keys.
  Closed JSON returns bytes without logging or parsing.

- [ ] **Step 7: Run GREEN and static checks**

  Run:

      uv run --frozen pytest tests/local_pilot_runtime/test_secret_provider.py -q
      uv run --frozen ruff check services/local_pilot_runtime/secret_provider.py tests/local_pilot_runtime/test_secret_provider.py
      uv run --frozen ruff format --check services/local_pilot_runtime/secret_provider.py tests/local_pilot_runtime/test_secret_provider.py
      uv run --frozen mypy services/local_pilot_runtime/secret_provider.py

- [ ] **Step 8: Commit exact paths**

  Commit message:

      feat(runtime): add mounted secret provider

### Task 2: Route common runtime secrets through the provider

**Files:**

- Modify: services/local_pilot_runtime/runtime_support.py
- Modify: tests/local_pilot_runtime/test_runtime_support.py
- Modify: services/local_pilot_runtime/projection_config.py
- Modify: tests/local_pilot_runtime/test_projection_config.py

- [ ] **Step 1: Add RED tests proving old direct file reads are delegated**

  Inject a recording provider and prove PostgreSQL passwords, bearer tokens and
  projection connection passwords request only their closed logical names.

- [ ] **Step 2: Preserve existing public APIs**

  load_secret_file remains a compatibility function but delegates to a
  MountedFileSecretProvider spec. Existing callers do not receive a filesystem
  path or provider-specific reference.

- [ ] **Step 3: Reject plaintext secret environment variables before provider access**

  Keep the current fail-closed environment scan and add deployment-mode tests
  for API keys, passwords, tokens and bearer values.

- [ ] **Step 4: Run focused and related tests**

  Run:

      uv run --frozen pytest tests/local_pilot_runtime/test_secret_provider.py tests/local_pilot_runtime/test_runtime_support.py tests/local_pilot_runtime/test_projection_config.py -q

- [ ] **Step 5: Commit exact paths**

  Commit message:

      refactor(runtime): centralize mounted secret reads

### Task 3: Route domain byte and JSON credentials through the provider

**Files:**

- Modify: services/local_pilot_runtime/channel_config.py
- Modify: services/local_pilot_runtime/trusted_phrase_lexicon.py
- Modify: services/observer/observer/identity_tokens.py
- Modify: services/model_gateway/tokenization.py
- Modify: tests/local_pilot_runtime/test_channel_config.py
- Modify: tests/local_pilot_runtime/test_trusted_phrase_lexicon.py
- Modify: tests/observer/test_identity_tokens.py
- Modify: tests/model_gateway/test_tokenization.py

- [ ] **Step 1: Add RED tests for provider-returned bytes**

  Cover full Email closed JSON, trusted phrase JSON, exact 32-byte identity HMAC,
  tokenizer HMAC and AES-256 mapping-vault keys.

- [ ] **Step 2: Keep domain schema validation after the provider boundary**

  The provider proves file safety and size. Each domain still rejects duplicate
  JSON keys, additional fields, incorrect site binding, invalid key length and
  unsafe values.

- [ ] **Step 3: Verify no secret value reaches repr or errors**

  Assertions must inspect every public wrapper and representative failure.

- [ ] **Step 4: Run service regressions**

  Run:

      uv run --frozen pytest tests/local_pilot_runtime tests/observer/test_identity_tokens.py tests/model_gateway -q
      uv run --frozen mypy services/local_pilot_runtime services/observer/observer services/model_gateway

- [ ] **Step 5: Commit exact paths**

  Commit message:

      refactor(runtime): unify domain secret inputs

## Chunk 2: Deployment Contract and Preflight

### Task 4: Freeze a value-free deployment projection contract

**Files:**

- Create: contracts/gate6/deployment-secret-projection-v1.0.schema.json
- Create: contracts/examples/gate6/deployment-secret-projection-valid.json
- Create: contracts/examples/gate6/deployment-secret-projection-invalid-secret-value.json
- Create: tests/contracts/test_deployment_secret_projection.py

- [ ] **Step 1: Write RED contract tests**

  Require site, environment, logical name, target filename, kind, size boundary,
  component, required state and non-secret platform version identifier. Reject
  value, token, password, keychain_ref, secret hash, URI and arbitrary fields.

- [ ] **Step 2: Add the closed JSON Schema and examples**

  The contract contains metadata only and binds each logical name to one fixed
  /run/secrets filename.

- [ ] **Step 3: Run the complete contract suite**

  Run:

      uv run --frozen pytest tests/contracts -q

- [ ] **Step 4: Commit exact paths**

  Commit message:

      feat(contracts): freeze deployment secret projection

### Task 5: Add deployment-mode secret preflight

**Files:**

- Create: services/local_pilot_runtime/deployment_secret_preflight.py
- Create: tests/local_pilot_runtime/test_deployment_secret_preflight.py
- Create: scripts/deploy/preflight-secrets
- Create: tests/infra/test_deployment_secret_preflight.py

- [ ] **Step 1: Capture RED for missing module and launcher**

  The preflight must fail before DB, connector, provider or Frappe access.

- [ ] **Step 2: Validate metadata and all required mounted files**

  Load the contract, bind site/environment, construct closed SecretSpec values,
  read every required mount through MountedFileSecretProvider and emit only
  stable status codes.

- [ ] **Step 3: Reject local-only inputs**

  Deployment mode rejects keychain URI, macOS security tooling, plaintext env
  secrets, provider payloads and repository-contained secret files.

- [ ] **Step 4: Test exact execution order**

  Prove the preflight completes before any application server, database
  connector or network factory is invoked.

- [ ] **Step 5: Run focused and infrastructure tests**

  Run:

      uv run --frozen pytest tests/local_pilot_runtime/test_deployment_secret_preflight.py tests/infra/test_deployment_secret_preflight.py -q
      bash -n scripts/deploy/preflight-secrets
      uv run --frozen ruff check services/local_pilot_runtime/deployment_secret_preflight.py tests/local_pilot_runtime/test_deployment_secret_preflight.py tests/infra/test_deployment_secret_preflight.py

- [ ] **Step 6: Commit exact paths**

  Commit message:

      feat(deploy): preflight mounted runtime secrets

### Task 6: Document platform projection and rotation

**Files:**

- Create: infra/prod/secret-provider-v1.template.json
- Create: docs/deployment-secrets.md
- Modify: docs/external-deps.md
- Modify: docs/governance/threat-model.md
- Create: tests/governance/test_deployment_secret_truth.py

- [ ] **Step 1: Add RED documentation truth tests**

  Require local Keychain-only wording, platform-managed version identifiers,
  regular-file projection, read-only mounts, restart-bound rotation, rollback
  window and explicit no-env/no-image/no-repository rules.

- [ ] **Step 2: Add a value-free production template**

  Include logical names and target filenames only. Provider-specific resource
  identifiers remain external until the platform is selected.

- [ ] **Step 3: Document platform adapter choices**

  Describe managed container secrets, Kubernetes CSI or External Secrets with
  private regular-file projection, and Vault Agent. Mark the selected adapter
  as blocked_platform_selection until the operator chooses the deployment
  platform.

- [ ] **Step 4: Document rotation and emergency rollback**

  New version, private projection, preflight, bounded rollout, health proof,
  old-version revocation and rollback steps must be explicit.

- [ ] **Step 5: Run governance and full verification**

  Run:

      uv run --frozen pytest tests/governance tests/infra tests/contracts tests/local_pilot_runtime -q
      uv run --frozen ruff check .
      uv run --frozen ruff format --check .
      uv run --frozen mypy services
      python -m compileall -q apps services scripts tests
      git diff --check

- [ ] **Step 6: Commit exact paths**

  Commit message:

      docs(deploy): define managed secret lifecycle

## Chunk 3: Platform Adapter Gate

### Task 7: Select and implement one platform adapter

**Design-only decision (2026-08-11):** The user selected Tencent Cloud for a
future deployment and retained the Mac as the current pilot. The selected
future pattern is TKE ServiceAccount OIDC + SSM + External Secrets +
KMS-encrypted Kubernetes Secret + startup projection into a memory-backed
`emptyDir` containing 0400 regular files. The application will mount only that
destination read-only at `/run/secrets`.

The user explicitly selected **record the design only**. No Tencent Cloud
resource, vendor manifest, IAM/CAM role, SSM secret, adapter implementation, or
cloud test is authorized by this plan update. A future implementation starts
only after a separate approval.

- [x] **Step 1: Record the selected platform and workload identity design**
- [ ] **Step 2: Add provider-specific projection manifests without values**
- [ ] **Step 3: Prove private regular-file projection and read-only app mounts**
- [ ] **Step 4: Run rotation and rollback in an isolated environment**
- [ ] **Step 5: Capture platform audit evidence without secret values**
- [ ] **Step 6: Keep production Go false until backup, monitoring, privacy and
  release approvals are separately complete**
