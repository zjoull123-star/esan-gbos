from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from services.email_gateway.identity_projection import IdentityProjectionService
from services.email_gateway.repositories.identity import InMemoryIdentityProjectionRepository

SITE = "alpha.example"
PURPOSE = "sales_follow_up"
OPAQUE = "extid:v1:email:" + "A" * 43
ROOT = Path(__file__).resolve().parents[2]


class _UnusedIntake:
    def accept(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("publication intake must not be called")


def _payload() -> dict[str, object]:
    from services.email_gateway.identity_projection import projection_receipt

    values: dict[str, object] = {
        "site_id": SITE,
        "processing_purpose": PURPOSE,
        "opaque_address_ref": OPAQUE,
        "external_identity_ref": "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "external_identity_revision": 3,
        "identity_type": "Party",
        "team_ref": "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "status": "confirmed",
        "observed_at": "2026-08-14T09:30:00Z",
    }
    return {**values, "projection_receipt": projection_receipt(values)}


def _digest(value: object) -> str:
    from services.email_gateway.models import canonical_digest

    return canonical_digest(value)


def _with_receipt(value: dict[str, object]) -> dict[str, object]:
    from services.email_gateway.identity_projection import projection_receipt

    fields = {key: item for key, item in value.items() if key != "projection_receipt"}
    return {**fields, "projection_receipt": projection_receipt(fields)}


def _app():
    from services.email_gateway.api import create_email_gateway_app

    repository = InMemoryIdentityProjectionRepository()
    application = create_email_gateway_app(
        intake=_UnusedIntake(),  # type: ignore[arg-type]
        publication_bearer_token="publication-secret",
        publication_auth_ref="observer-email-publication-v1",
        identity_projection_service=IdentityProjectionService(repository),
        identity_projection_bearer_token="identity-projection-secret",
        identity_projection_auth_ref="observer-identity-projection-v1",
    )
    return application, repository


def _headers(payload: object) -> dict[str, str]:
    assert isinstance(payload, dict)
    projection_receipt = payload.get("projection_receipt")
    assert isinstance(projection_receipt, str)
    return {
        "Authorization": "Bearer identity-projection-secret",
        "X-GBOS-Local-Auth-Ref": "observer-identity-projection-v1",
        "X-Site-ID": SITE,
        "X-Processing-Purpose": PURPOSE,
        "X-Payload-Digest": _digest(payload),
        "X-Request-ID": "identity-projection:" + projection_receipt.removeprefix("sha256:"),
    }


def test_identity_projection_accepts_and_replays_exact_delivery() -> None:
    app, repository = _app()
    payload = _payload()

    first = TestClient(app).post(
        "/internal/v1/identity-projections/accept",
        json=payload,
        headers=_headers(payload),
    )
    replay = TestClient(app).post(
        "/internal/v1/identity-projections/accept",
        json=payload,
        headers=_headers(payload),
    )

    expected = {
        "schema_version": "1.0",
        "projection_receipt": payload["projection_receipt"],
        "payload_digest": _digest(payload),
    }
    assert first.status_code == 200
    assert replay.json() == first.json() == expected
    assert first.headers["cache-control"] == "no-store"
    stored = repository.get(
        __import__("services.email_gateway.models", fromlist=["TenantScope"]).TenantScope(
            SITE, PURPOSE
        ),
        OPAQUE,
    )
    assert stored is not None and stored.external_identity_revision == 3
    assert "target" not in repr(first.json()).lower()


def test_identity_projection_rejects_auth_scope_digest_receipt_and_extra_fields() -> None:
    app, repository = _app()
    baseline = _payload()
    cases: list[tuple[dict[str, object], dict[str, str]]] = []
    cases.append((baseline, {**_headers(baseline), "Authorization": "Bearer wrong"}))
    cases.append((baseline, {**_headers(baseline), "X-Site-ID": "other.example"}))
    cases.append((baseline, {**_headers(baseline), "X-Processing-Purpose": "customer_service"}))
    cases.append((baseline, {**_headers(baseline), "X-Request-ID": "identity-projection:wrong"}))
    cases.append((baseline, {**_headers(baseline), "X-Payload-Digest": "sha256:" + "0" * 64}))
    extra = {**baseline, "target_ref": "protected-target@example.invalid"}
    cases.append((extra, _headers(extra)))
    wrong_receipt = {**baseline, "projection_receipt": "sha256:" + "f" * 64}
    cases.append((wrong_receipt, _headers(wrong_receipt)))
    non_email = _with_receipt({**baseline, "opaque_address_ref": "extid:v1:wecom:" + "A" * 43})
    cases.append((non_email, _headers(non_email)))
    excessive_revision = _with_receipt({**baseline, "external_identity_revision": 2_147_483_648})
    cases.append((excessive_revision, _headers(excessive_revision)))
    excessive_timestamp = _with_receipt(
        {**baseline, "observed_at": "2026-08-14T09:30:00.000000000000+00:00"}
    )
    cases.append((excessive_timestamp, _headers(excessive_timestamp)))

    for payload, headers in cases:
        response = TestClient(app).post(
            "/internal/v1/identity-projections/accept", json=payload, headers=headers
        )
        assert response.status_code in {400, 401, 403}
        assert "protected-target" not in response.text

    scope_type = __import__("services.email_gateway.models", fromlist=["TenantScope"]).TenantScope
    assert repository.get(scope_type(SITE, PURPOSE), OPAQUE) is None


def test_identity_projection_rejects_same_revision_drift_and_stale_revision() -> None:
    app, _repository = _app()
    current = _payload()
    assert (
        TestClient(app)
        .post("/internal/v1/identity-projections/accept", json=current, headers=_headers(current))
        .status_code
        == 200
    )

    drift_fields = {key: value for key, value in current.items() if key != "projection_receipt"}
    drift_fields["status"] = "revoked"
    from services.email_gateway.identity_projection import projection_receipt

    drift = {**drift_fields, "projection_receipt": projection_receipt(drift_fields)}
    drift_response = TestClient(app).post(
        "/internal/v1/identity-projections/accept", json=drift, headers=_headers(drift)
    )
    assert drift_response.status_code == 409

    newer_fields = deepcopy(drift_fields)
    newer_fields["external_identity_revision"] = 4
    newer_fields["observed_at"] = (
        datetime(2026, 8, 14, 9, 31, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    )
    newer = {**newer_fields, "projection_receipt": projection_receipt(newer_fields)}
    assert (
        TestClient(app)
        .post("/internal/v1/identity-projections/accept", json=newer, headers=_headers(newer))
        .status_code
        == 200
    )
    stale_response = TestClient(app).post(
        "/internal/v1/identity-projections/accept", json=current, headers=_headers(current)
    )
    assert stale_response.status_code == 409


def test_governed_inbox_identity_reads_are_scoped_to_mailbox_business_purpose() -> None:
    source = (ROOT / "services/email_gateway/api.py").read_text(encoding="utf-8")

    assert source.count("projection.processing_purpose = mailbox.business_purpose") == 2
