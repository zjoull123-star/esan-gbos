from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .models import AuthContext, QueryPlan, ValidatedRequest

_REQUEST_KEYS = frozenset(
    {
        "request_id",
        "site_id",
        "account_set_ref",
        "processing_purpose",
        "logical_object",
        "limit",
        "offset",
        "timeout_ms",
    }
)
_SAFE_REQUEST_ID = re.compile(r"^[a-z][a-z0-9._:-]{7,127}$")
_SAFE_SITE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,139}$")
_SAFE_ACCOUNT_SET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_PURPOSE = re.compile(r"^[a-z][a-z0-9_]{2,127}$")
_MAX_TIMEOUT_MS = 30_000


class RequestRejected(ValueError):
    """A request was denied before a transport could be invoked."""


@dataclass(frozen=True, slots=True)
class FrozenKingdeePolicy:
    allowlist_version: str
    dictionary_version: str
    max_rows: int
    read_tools: frozenset[str]
    tools: frozenset[str]
    forbidden_operation_tokens: frozenset[str]
    _tool_to_object: Mapping[str, str]
    _object_plans: Mapping[str, QueryPlan]

    @classmethod
    def load(cls, *, contracts_root: Path | None = None) -> FrozenKingdeePolicy:
        root = contracts_root or Path(__file__).resolve().parents[2] / "contracts"
        gate2 = root / "gate2"
        allowlist = _read_object(gate2 / "kingdee-query-allowlist-v0.json")
        manifest = _read_object(gate2 / "mcp-tool-manifest-v0.json")
        dictionary = _read_object(gate2 / "kingdee-field-dictionary-v0.json")

        allowlist_tools = _tool_mapping(allowlist.get("tools"), source="allowlist")
        manifest_tools = _tool_mapping(manifest.get("tools"), source="manifest")
        if allowlist_tools != manifest_tools:
            raise ValueError("Gate 2 Kingdee tool contracts disagree")
        if any(not tool.endswith(".get") for tool in allowlist_tools):
            raise ValueError("frozen Kingdee surface contains a non-read tool")
        if any(item.get("mutates") is not False for item in _objects(manifest.get("tools"))):
            raise ValueError("frozen Kingdee manifest contains a mutating tool")

        object_plans = _dictionary_plans(dictionary.get("objects"), allowlist_tools)
        if set(object_plans) != set(allowlist_tools.values()):
            raise ValueError("Gate 2 Kingdee dictionary and tool objects disagree")

        max_rows = allowlist.get("max_rows")
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows != 50:
            raise ValueError("frozen Kingdee max_rows must be exactly 50")
        forbidden_tokens_value = allowlist.get("forbidden_operation_tokens")
        if not isinstance(forbidden_tokens_value, list) or not all(
            isinstance(value, str) and value for value in forbidden_tokens_value
        ):
            raise ValueError("invalid frozen forbidden operation tokens")
        forbidden_tokens = frozenset(forbidden_tokens_value)
        required_forbidden = {
            "create",
            "update",
            "save",
            "submit",
            "audit",
            "unaudit",
            "delete",
            "payment",
            "write",
            "execute",
            "push",
        }
        if not required_forbidden <= forbidden_tokens:
            raise ValueError("frozen forbidden operation tokens are incomplete")

        allowlist_version = allowlist.get("allowlist_version")
        dictionary_version = dictionary.get("dictionary_version")
        if not isinstance(allowlist_version, str) or not isinstance(dictionary_version, str):
            raise ValueError("invalid frozen Kingdee contract versions")
        read_tools = frozenset(allowlist_tools)
        return cls(
            allowlist_version=allowlist_version,
            dictionary_version=dictionary_version,
            max_rows=max_rows,
            read_tools=read_tools,
            tools=frozenset({"metadata.get", *read_tools}),
            forbidden_operation_tokens=forbidden_tokens,
            _tool_to_object=MappingProxyType(allowlist_tools),
            _object_plans=MappingProxyType(object_plans),
        )

    def plan_for(self, tool_name: str, logical_object: str) -> QueryPlan:
        self._reject_mutation_shape(tool_name)
        if logical_object not in self._object_plans:
            raise RequestRejected("unsupported logical object")
        if tool_name == "metadata.get":
            base = self._object_plans[logical_object]
            return QueryPlan(
                tool_name=tool_name,
                logical_object=base.logical_object,
                source_form=base.source_form,
                fields=base.fields,
                source_fields=base.source_fields,
                field_types=base.field_types,
                filters=base.filters,
                order=base.order,
            )
        expected = self._tool_to_object.get(tool_name)
        if expected is None:
            raise RequestRejected("unsupported Kingdee read tool")
        if expected != logical_object:
            raise RequestRejected("tool and logical object do not match")
        return self._object_plans[logical_object]

    def validate_request(
        self,
        request: Mapping[str, Any],
        *,
        auth: AuthContext,
    ) -> ValidatedRequest:
        if not isinstance(request, Mapping):
            raise RequestRejected("request must be an object")
        observed_keys = frozenset(request)
        if observed_keys != _REQUEST_KEYS or any(not isinstance(key, str) for key in request):
            missing = sorted(_REQUEST_KEYS - observed_keys)
            unknown = sorted(str(key) for key in observed_keys - _REQUEST_KEYS)
            raise RequestRejected(f"invalid request keys: missing={missing}, unknown={unknown}")
        if not auth.authenticated:
            raise RequestRejected("authenticated request required")
        if "kingdee-read" not in auth.granted_scopes:
            raise RequestRejected("kingdee-read scope required")

        request_id = _match(request["request_id"], _SAFE_REQUEST_ID, "request_id")
        site_id = _match(request["site_id"], _SAFE_SITE_ID, "site_id")
        account_set_ref = _match(
            request["account_set_ref"],
            _SAFE_ACCOUNT_SET,
            "account_set_ref",
        )
        processing_purpose = _match(
            request["processing_purpose"],
            _SAFE_PURPOSE,
            "processing_purpose",
        )
        logical_object = request["logical_object"]
        if not isinstance(logical_object, str) or logical_object not in self._object_plans:
            raise RequestRejected("unsupported logical_object")
        limit = _bounded_int(request["limit"], name="limit", minimum=1, maximum=self.max_rows)
        offset = _bounded_int(request["offset"], name="offset", minimum=0, maximum=1_000_000)
        timeout_ms = _bounded_int(
            request["timeout_ms"],
            name="timeout_ms",
            minimum=1,
            maximum=_MAX_TIMEOUT_MS,
        )
        return ValidatedRequest(
            request_id=request_id,
            site_id=site_id,
            account_set_ref=account_set_ref,
            processing_purpose=processing_purpose,
            logical_object=logical_object,
            limit=limit,
            offset=offset,
            timeout_ms=timeout_ms,
        )

    def _reject_mutation_shape(self, tool_name: str) -> None:
        if not isinstance(tool_name, str) or not tool_name:
            raise RequestRejected("tool_name must be a non-empty string")
        tokens = frozenset(part for part in re.split(r"[^a-z0-9]+", tool_name.lower()) if part)
        if tokens & self.forbidden_operation_tokens:
            raise RequestRejected("forbidden mutation-shaped Kingdee tool")


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load frozen Kingdee contract: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"frozen Kingdee contract must be an object: {path.name}")
    return value


def _objects(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("frozen Kingdee contract list is invalid")
    return value


def _tool_mapping(value: object, *, source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _objects(value):
        name = item.get("name")
        logical_object = item.get("logical_object")
        if not isinstance(name, str) or not isinstance(logical_object, str):
            raise ValueError(f"invalid {source} tool entry")
        if name in result:
            raise ValueError(f"duplicate {source} tool")
        result[name] = logical_object
    if len(result) != 7:
        raise ValueError(f"{source} must freeze exactly seven Kingdee read tools")
    return result


def _dictionary_plans(
    value: object,
    tool_to_object: Mapping[str, str],
) -> dict[str, QueryPlan]:
    object_to_tool = {logical_object: tool for tool, logical_object in tool_to_object.items()}
    result: dict[str, QueryPlan] = {}
    for item in _objects(value):
        logical_object = item.get("logical_object")
        candidate_form = item.get("candidate_form")
        fields_value = item.get("fields")
        if (
            not isinstance(logical_object, str)
            or not isinstance(candidate_form, dict)
            or candidate_form.get("verification_status") != "gate5_metadata_required"
        ):
            raise ValueError("invalid frozen Kingdee dictionary object")
        source_form = candidate_form.get("value")
        if not isinstance(source_form, str) or not source_form:
            raise ValueError("invalid frozen Kingdee candidate form")
        fields = _objects(fields_value)
        logical_fields: list[str] = []
        source_fields: list[str] = []
        field_types: list[str] = []
        for field in fields:
            logical_name = field.get("logical_name")
            data_type = field.get("data_type")
            candidate = field.get("candidate_field")
            if (
                not isinstance(logical_name, str)
                or not isinstance(data_type, str)
                or not isinstance(candidate, dict)
                or candidate.get("verification_status") != "gate5_metadata_required"
                or not isinstance(candidate.get("value"), str)
            ):
                raise ValueError("invalid frozen Kingdee dictionary field")
            logical_fields.append(logical_name)
            source_fields.append(candidate["value"])
            field_types.append(data_type)
        if not logical_fields or len(logical_fields) != len(set(logical_fields)):
            raise ValueError("frozen Kingdee fields must be unique and non-empty")
        tool_name = object_to_tool.get(logical_object)
        if tool_name is None:
            raise ValueError("dictionary contains an object without a frozen read tool")
        result[logical_object] = QueryPlan(
            tool_name=tool_name,
            logical_object=logical_object,
            source_form=source_form,
            fields=tuple(logical_fields),
            source_fields=tuple(source_fields),
            field_types=tuple(field_types),
            filters=(),
            order=((logical_fields[0], "asc"),),
        )
    return result


def _match(value: object, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RequestRejected(f"invalid {name}")
    return value


def _bounded_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise RequestRejected(f"{name} must be an integer in {minimum}..{maximum}")
    return value
