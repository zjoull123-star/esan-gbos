# Exact custom-image build

The image flow has two explicit stages. Neither stage accepts a moving source
branch.

## Gate 0 upstream-only image

```bash
scripts/dev/build-custom-image \
  --stage upstream \
  --image esan-gbos-upstream:gate0
```

The script verifies the Frappe, ERPNext, CRM, and frappe_docker release tags
against their frozen 40-character commits before building for `linux/arm64`
by default.
It passes [`apps.upstream.json`](../../infra/dev/apps.upstream.json) as a
BuildKit secret and prints the local image digest plus `bench version`.

If the exact build fails, it retries once with identical refs and arguments.
The retry handles transient package-network hangs; a second failure exits
nonzero and must not trigger a version downgrade.

To run only this three-app upstream stage:

```bash
scripts/dev/bootstrap --upstream-only
```

That flag explicitly sets `APP_LIST=erpnext,crm`. It is not final Gate 1
evidence.

## Gate 1 final monorepo image

`esan_gbos` lives below `apps/esan_gbos` in this monorepo, so it is not added
to the upstream `apps.json` as though the repository root were a Frappe app.
The final build copies the local app from the exact repository commit:

```bash
scripts/dev/build-custom-image \
  --stage final \
  --esan-commit 0123456789abcdef0123456789abcdef01234567 \
  --image esan-gbos-final:gate1
```

Replace the sample SHA with the current full commit. The script fails closed
unless:

- `apps/esan_gbos/pyproject.toml` and `esan_gbos/hooks.py` exist;
- those files are tracked;
- the whole app tree is clean;
- the supplied commit equals repository `HEAD`.

The build context is produced with `git archive` from that commit, so ignored
caches and unrelated monorepo changes cannot enter the final image.

The exact frappe_docker builder stage supplies Node 24.13.0 for
`bench build --app esan_gbos`. The final backend stage copies the built assets
and Python app, runs only the Python editable install, and does not assume Node
exists at runtime.

The manual GitHub fresh-site smoke runs on `linux/amd64`. An explicitly
authorized registry publisher can create its single-platform prerequisite
without changing the local ARM64 default:

```bash
scripts/dev/build-custom-image \
  --stage final \
  --esan-commit 0123456789abcdef0123456789abcdef01234567 \
  --platform linux/amd64 \
  --push \
  --image registry.example/esan/esan-gbos:gate1-0123456
```

`--push` accepts only a registry-qualified final-image tag. The smoke workflow
then accepts the immutable `repository@sha256:digest` and first proves it can
be pulled for `linux/amd64`. Publishing a registry image is not part of the
default local bootstrap and must be separately authorized.

Default `scripts/dev/bootstrap` is the final gate and requires
`APP_LIST=erpnext,crm,esan_gbos`. Site creation fails before installation if
any required app is absent from the image. All published development ports
remain bound to loopback, and the script refuses a production-enabled
environment.
