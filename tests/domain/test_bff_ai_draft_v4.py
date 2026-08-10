from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator, Mapping
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


class _Document(dict[str, Any]):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


class _Frappe(ModuleType):
    class PermissionError(Exception):
        pass

    class DoesNotExistError(Exception):
        pass

    class ValidationError(Exception):
        pass

    class DuplicateEntryError(Exception):
        pass

    def __init__(self) -> None:
        super().__init__("frappe")
        self.local = SimpleNamespace(
            site="gbos.localhost",
            gbos_request_id="REQ-ai-draft-001",
            request=SimpleNamespace(method="GET", headers={}),
            response={},
        )
        self.session = SimpleNamespace(user="reviewer@example.invalid")
        self.db = SimpleNamespace(rollback=lambda: None)
        self.documents: dict[tuple[str, str], _Document] = {}

    @staticmethod
    def whitelist(
        *_args: Any, **_kwargs: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return lambda function: function

    def get_doc(self, doctype: str, name: str, **_kwargs: Any) -> _Document:
        return self.documents[(doctype, name)]


@pytest.fixture
def ai_draft_module() -> Iterator[tuple[Any, _Frappe]]:
    fake_frappe = _Frappe()
    module_names = (
        "esan_gbos.api.v1.audit",
        "esan_gbos.api.v1.common",
        "esan_gbos.api.v4.gateway",
        "esan_gbos.api.v4.ai_draft",
    )
    originals = {name: sys.modules.pop(name, None) for name in module_names}
    original_frappe = sys.modules.get("frappe")
    sys.modules["frappe"] = fake_frappe
    module = importlib.import_module("esan_gbos.api.v4.ai_draft")
    yield module, fake_frappe
    for name in reversed(module_names):
        sys.modules.pop(name, None)
        if originals[name] is not None:
            sys.modules[name] = originals[name]
    if original_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original_frappe


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "doctype": "GBOS Informal Observation",
        "draft_id": "OBS-DRAFT-01",
        "kind": "CEO Informal Observation",
        "status": "AI Draft",
        "origin": "AI",
        "origin_reference": "OBSERVATION-01",
        "subject": "客户询问交期",
        "revision": 1,
        "modified": "2026-08-11T01:00:00+00:00",
    }
    row.update(overrides)
    return row


def _informal_document(**overrides: Any) -> _Document:
    values: dict[str, Any] = {
        "doctype": "GBOS Informal Observation",
        "name": "OBS-DRAFT-01",
        "origin": "AI",
        "origin_reference": "OBSERVATION-01",
        "review_status": "AI Draft",
        "subject": "客户询问交期",
        "revision": 1,
        "model_name": "deepseek-v4-flash",
        "model_version": "communication-intelligence-local-pilot-v1",
        "evidence_refs": [
            {
                "evidence_ref": "evidence://gbos.localhost/email/sha256-a",
                "locator_ref": "bytes://0-127",
            }
        ],
    }
    values.update(overrides)
    return _Document(values)


def test_context_informal_draft_enriches_from_governed_frappe_fields(
    ai_draft_module: tuple[Any, _Frappe],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake_frappe = ai_draft_module
    fake_frappe.documents[("GBOS Informal Observation", "OBS-DRAFT-01")] = _informal_document()
    monkeypatch.setattr(
        module,
        "call_local",
        lambda *_args, **_kwargs: pytest.fail("Context drafts must not query Agent proposals"),
    )

    result = module._enrich([_row()])

    assert result == [
        {
            "draft_id": "OBS-DRAFT-01",
            "kind": "CEO Informal Observation",
            "status": "AI Draft",
            "origin": "AI",
            "subject": "客户询问交期",
            "evidence": [
                {
                    "ref": "evidence://gbos.localhost/email/sha256-a",
                    "locator": "bytes://0-127",
                }
            ],
            "model": {
                "name": "deepseek-v4-flash",
                "version": "communication-intelligence-local-pilot-v1",
            },
            "revision": 1,
        }
    ]


def test_context_informal_draft_rejects_origin_binding_drift(
    ai_draft_module: tuple[Any, _Frappe],
) -> None:
    module, fake_frappe = ai_draft_module
    fake_frappe.documents[("GBOS Informal Observation", "OBS-DRAFT-01")] = _informal_document(
        origin_reference="OBSERVATION-OTHER"
    )

    with pytest.raises(module.BFFError, match="invalid") as raised:
        module._enrich([_row()])

    assert raised.value.status == 503


def test_context_informal_draft_rejects_missing_model_identity(
    ai_draft_module: tuple[Any, _Frappe],
) -> None:
    module, fake_frappe = ai_draft_module
    fake_frappe.documents[("GBOS Informal Observation", "OBS-DRAFT-01")] = _informal_document(
        model_name=None
    )

    with pytest.raises(module.BFFError, match="invalid") as raised:
        module._enrich([_row()])

    assert raised.value.status == 503


def test_agent_origin_draft_keeps_agent_enrichment_path(
    ai_draft_module: tuple[Any, _Frappe],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake_frappe = ai_draft_module
    calls: list[Mapping[str, Any]] = []

    def call_local(service: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"service": service, **kwargs})
        return {
            "draft": {
                "draft_id": "PROPOSAL-01",
                "evidence": [{"ref": "evidence://EVD-01", "locator": "bytes://0-1"}],
                "model": {"name": "deepseek-v4-flash", "version": "agent-v1"},
            }
        }

    monkeypatch.setattr(module, "call_local", call_local)
    monkeypatch.setattr(
        fake_frappe,
        "get_doc",
        lambda *_args, **_kwargs: pytest.fail("Agent drafts must not load Context documents"),
    )

    result = module._enrich(
        [
            _row(
                doctype="GBOS Work Item",
                draft_id="WRK-01",
                kind="Work Item",
                origin_reference="PROPOSAL-01",
                subject="联系客户",
            )
        ]
    )

    assert result[0]["draft_id"] == "WRK-01"
    assert calls == [
        {
            "service": "Agent",
            "method": "GET",
            "path": "/internal/v1/ai-drafts/PROPOSAL-01",
            "purpose": "ai_draft_review",
        }
    ]
