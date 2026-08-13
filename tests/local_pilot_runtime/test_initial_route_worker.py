from __future__ import annotations

from pathlib import Path


def test_initial_route_worker_is_import_safe_and_default_disabled(tmp_path: Path) -> None:
    from services.local_pilot_runtime.initial_route_worker import main

    assert (
        main(
            manifest_path=tmp_path / "missing-manifest.json",
            config_path=tmp_path / "missing-config.json",
            environ={},
            connector=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("database must not open")
            ),
        )
        == 78
    )


def test_http_transport_is_proxy_free_redirect_free_and_exact_url(monkeypatch) -> None:
    from services.email_gateway.initial_routing import FRAPPE_INITIAL_ROUTE_URL
    from services.local_pilot_runtime.initial_route_worker import HttpxInitialRouteTransport

    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        content = (
            b'{"message":{"route_authority":{"route_status":"unassigned",'
            b'"safe_reason_code":"owner_unavailable",'
            b'"resolved_at":"2026-08-14T09:30:00+00:00"}}}'
        )

        def json(self):
            import json

            return json.loads(self.content)

    class Client:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, **kwargs: object) -> Response:
            captured["url"] = url
            return Response()

    monkeypatch.setattr("services.local_pilot_runtime.initial_route_worker.httpx.Client", Client)
    status, _body = HttpxInitialRouteTransport().post(
        url=FRAPPE_INITIAL_ROUTE_URL,
        headers={"Authorization": "redacted"},
        payload={"payload": {}},
        timeout_seconds=3,
    )
    assert status == 200
    assert captured["url"] == FRAPPE_INITIAL_ROUTE_URL
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
