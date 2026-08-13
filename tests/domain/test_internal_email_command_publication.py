from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROLE = "Email Command Publication Consumer"
USER = "email-command-publication@localhost.invalid"
AUTH_REF = "email-command-publication-v1"
PURPOSE = "email_command_publication"
PUBLICATION = "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV"
WORKER = "email-command-relay-01"
NOW = datetime(2026, 8, 13, 13, 5, tzinfo=UTC)
ROOT = Path(__file__).parents[2]


class _PermissionError(Exception):
    pass


class _Document:
    def __init__(self, runtime: _Frappe, values: dict[str, Any]) -> None:
        object.__setattr__(self, "runtime", runtime)
        object.__setattr__(self, "values", dict(values))
        object.__setattr__(self, "flags", SimpleNamespace())

    def __getattr__(self, key: str) -> Any:
        try:
            return self.values[key]
        except KeyError:
            return None

    def __setattr__(self, key: str, value: Any) -> None:
        self.values[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def save(self, *, ignore_permissions: bool) -> _Document:
        assert ignore_permissions is True
        if self.runtime.fail_save:
            raise RuntimeError("database write failed")
        self.runtime.rows[self.name] = self
        return self


class _Database:
    def __init__(self, runtime: _Frappe) -> None:
        self.runtime = runtime
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.PermissionError = _PermissionError
        self.session = SimpleNamespace(user=USER)
        self.local = SimpleNamespace(
            site="gbos.localhost",
            response={},
            request=SimpleNamespace(
                method="POST",
                headers={
                    "Authorization": "token publication-key:publication-secret",
                    "X-Site-ID": "gbos.localhost",
                    "X-Processing-Purpose": PURPOSE,
                    "X-Request-ID": "publication-request-0001",
                    "X-GBOS-Frappe-Auth-Ref": AUTH_REF,
                },
            ),
        )
        self.conf = {
            "gbos_email_command_publication_identities": {
                AUTH_REF: {
                    "user": USER,
                    "site_id": "gbos.localhost",
                    "processing_purposes": [PURPOSE],
                }
            }
        }
        self.roles = {USER: {ROLE}}
        self.rows: dict[str, _Document] = {}
        self.fail_save = False
        self.db = _Database(self)

    def whitelist(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs

        def decorate(function: Any) -> Any:
            return function

        return decorate

    def get_roles(self, user: str | None = None) -> list[str]:
        return sorted(self.roles.get(user or self.session.user, set()))

    def parse_json(self, value: str) -> Any:
        return json.loads(value)

    def get_all(self, doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert doctype == "GBOS Command Publication"
        del kwargs
        return [{"name": name} for name in sorted(self.rows)]

    def get_doc(
        self,
        doctype: str,
        name: str,
        *,
        for_update: bool = False,
    ) -> _Document:
        assert doctype == "GBOS Command Publication"
        assert for_update is True
        return self.rows[name]


def _command() -> dict[str, Any]:
    value = json.loads(
        (
            ROOT
            / "contracts"
            / "email_gateway"
            / "examples"
            / "email-send-approved-command-v2.json"
        ).read_text(encoding="utf-8")
    )
    payload = {key: item for key, item in value.items() if key != "payload_sha256"}
    value["payload_sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return value


def _publication(fake: _Frappe) -> _Document:
    row = _Document(
        fake,
        {
            "doctype": "GBOS Command Publication",
            "name": PUBLICATION,
            "approved_command": "CMD-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "command_payload": json.dumps(_command(), sort_keys=True),
            "payload_digest": "sha256:" + _command()["payload_sha256"],
            "publication_status": "Pending",
            "attempt": 0,
            "generation": 0,
            "max_attempts": 5,
            "worker_id": None,
            "fence_token": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "claim_request_id": None,
            "heartbeat_request_id": None,
            "acknowledge_request_id": None,
            "release_request_id": None,
            "gateway_command_receipt_ref": None,
            "gateway_send_outbox_ref": None,
            "gateway_payload_digest": None,
            "safe_error_code": None,
        },
    )
    fake.rows[PUBLICATION] = row
    return row


@pytest.fixture
def publication_api() -> Generator[tuple[Any, _Frappe]]:
    fake = _Frappe()
    _publication(fake)
    names = (
        "frappe",
        "esan_gbos.api.internal.email_command_publication",
        "esan_gbos.domain.approved_command",
        "esan_gbos.domain.permissions",
    )
    originals = {name: sys.modules.get(name) for name in names}
    sys.modules["frappe"] = fake
    for name in names[1:]:
        sys.modules.pop(name, None)
    module = importlib.import_module("esan_gbos.api.internal.email_command_publication")
    module._now = lambda: NOW
    module._new_fence_token = lambda: "FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    yield module, fake
    for name, original in originals.items():
        sys.modules.pop(name, None)
        if original is not None:
            sys.modules[name] = original


def _claim_payload(**overrides: Any) -> dict[str, Any]:
    value = {
        "site_id": "gbos.localhost",
        "processing_purpose": PURPOSE,
        "worker_id": WORKER,
        "lease_seconds": 30,
        "request_id": "publication-request-0001",
    }
    value.update(overrides)
    return value


def _payload_digest() -> str:
    return "sha256:" + _command()["payload_sha256"]


def _claim(api: Any) -> dict[str, Any]:
    response = api.claim(_claim_payload())
    assert "publication" in response
    return response["publication"]


def _claim_identity(claim: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "site_id": "gbos.localhost",
        "processing_purpose": PURPOSE,
        "worker_id": WORKER,
        "publication_ref": claim["publication_ref"],
        "attempt": claim["attempt"],
        "generation": claim["generation"],
        "fence_token": claim["fence_token"],
        "request_id": request_id,
    }


def test_claim_is_fenced_no_store_and_response_loss_replay_is_stable(
    publication_api: tuple[Any, _Frappe],
) -> None:
    api, fake = publication_api

    first = api.claim(_claim_payload())
    replay = api.claim(_claim_payload())

    publication = first["publication"]
    assert replay == first
    assert publication == {
        "publication_ref": PUBLICATION,
        "attempt": 1,
        "generation": 1,
        "fence_token": "FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "lease_expires_at": "2026-08-13T13:05:30Z",
        "command": _command(),
        "payload_digest": _payload_digest(),
    }
    assert fake.rows[PUBLICATION].attempt == 1
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"


def test_claim_response_loss_replay_remains_stable_after_the_original_lease_expires(
    publication_api: tuple[Any, _Frappe],
) -> None:
    api, fake = publication_api
    first = api.claim(_claim_payload())
    api._now = lambda: NOW + timedelta(seconds=31)

    replay = api.claim(_claim_payload())

    assert replay == first
    assert fake.rows[PUBLICATION].attempt == 1
    assert fake.rows[PUBLICATION].generation == 1


def test_claim_empty_is_a_closed_receipt(publication_api: tuple[Any, _Frappe]) -> None:
    api, fake = publication_api
    fake.rows[PUBLICATION].publication_status = "Acknowledged"

    assert api.claim(_claim_payload()) == {"publication": None}


def test_heartbeat_acknowledge_and_release_require_the_exact_live_fence(
    publication_api: tuple[Any, _Frappe],
) -> None:
    api, fake = publication_api
    claimed = _claim(api)

    fake.local.request.headers["X-Request-ID"] = "publication-heartbeat-0001"
    heartbeat = api.heartbeat(
        {
            **_claim_identity(claimed, "publication-heartbeat-0001"),
            "lease_seconds": 45,
        }
    )
    assert heartbeat == {
        "lease": {
            "publication_ref": PUBLICATION,
            "attempt": 1,
            "generation": 1,
            "fence_token": claimed["fence_token"],
            "lease_expires_at": "2026-08-13T13:05:45Z",
        }
    }

    stale = {**_claim_identity(claimed, "publication-ack-0001"), "generation": 2}
    stale.update(
        command_receipt_ref="ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        send_outbox_ref="SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        payload_digest=_payload_digest(),
    )
    fake.local.request.headers["X-Request-ID"] = "publication-ack-0001"
    assert api.acknowledge(stale) == {"error": {"code": "claim_fence_mismatch"}}

    acknowledged = api.acknowledge(
        {
            **_claim_identity(claimed, "publication-ack-0001"),
            "command_receipt_ref": "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "send_outbox_ref": "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "payload_digest": _payload_digest(),
        }
    )
    assert acknowledged == {
        "acknowledgement": {
            "publication_ref": PUBLICATION,
            "command_receipt_ref": "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "send_outbox_ref": "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "payload_digest": _payload_digest(),
            "status": "acknowledged",
        }
    }


@pytest.mark.parametrize("method", ["heartbeat", "acknowledge"])
def test_heartbeat_and_acknowledge_reject_an_expired_claim_lease(
    publication_api: tuple[Any, _Frappe],
    method: str,
) -> None:
    api, fake = publication_api
    claimed = _claim(api)
    api._now = lambda: NOW + timedelta(seconds=31)
    request_id = f"publication-{method}-expired"
    fake.local.request.headers["X-Request-ID"] = request_id
    payload = _claim_identity(claimed, request_id)
    if method == "heartbeat":
        payload["lease_seconds"] = 30
    else:
        payload.update(
            command_receipt_ref="ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            send_outbox_ref="SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            payload_digest=_payload_digest(),
        )

    response = getattr(api, method)(payload)

    assert response == {"error": {"code": "claim_lease_expired"}}
    assert fake.local.response["http_status_code"] == 409


def test_release_uses_only_fixed_retry_or_dead_letter_safe_codes(
    publication_api: tuple[Any, _Frappe],
) -> None:
    api, fake = publication_api
    claimed = _claim(api)

    invalid = api.release(
        {
            **_claim_identity(claimed, "publication-release-0001"),
            "safe_code": "raw provider exception customer@example.invalid",
        }
    )
    assert invalid == {"error": {"code": "invalid_publication_request"}}

    fake.local.request.headers["X-Request-ID"] = "publication-release-0001"
    released = api.release(
        {
            **_claim_identity(claimed, "publication-release-0001"),
            "safe_code": "gateway_unavailable",
        }
    )
    assert released == {
        "release": {
            "publication_ref": PUBLICATION,
            "status": "retry",
            "safe_code": "gateway_unavailable",
        }
    }
    assert (
        api.release(
            {
                **_claim_identity(claimed, "publication-release-0001"),
                "safe_code": "gateway_unavailable",
            }
        )
        == released
    )
    assert fake.rows[PUBLICATION].publication_status == "Retry"


def test_exact_service_identity_site_purpose_headers_and_role_are_required(
    publication_api: tuple[Any, _Frappe],
) -> None:
    api, fake = publication_api

    fake.roles[USER].add("System Manager")
    assert api.claim(_claim_payload()) == {"error": {"code": "authentication_required"}}

    fake.roles[USER] = {ROLE}
    fake.local.request.headers["X-Site-ID"] = "other.localhost"
    assert api.claim(_claim_payload()) == {"error": {"code": "identity_scope_mismatch"}}
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"


def test_database_failure_rolls_back_and_returns_no_sensitive_exception(
    publication_api: tuple[Any, _Frappe],
) -> None:
    api, fake = publication_api
    fake.fail_save = True

    response = api.claim(_claim_payload())

    assert response == {"error": {"code": "internal_error"}}
    assert fake.db.rollbacks == 1
    assert "database write failed" not in repr(response)
