from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from referencing import Registry, Resource

from .models import RecordKind


class ContractValidationError(ValueError):
    """A Context Service record failed its frozen wire contract."""


class ContextContractValidator:
    _FILES = {
        RecordKind.EVIDENCE: "evidence-record.schema.json",
        RecordKind.FACT_PROPOSAL: "gate3/fact-proposal-record.schema.json",
        RecordKind.ENTITY_RESOLUTION_PROPOSAL: ("gate3/entity-resolution-proposal.schema.json"),
    }

    def __init__(self, contracts_root: Path) -> None:
        self._root = Path(contracts_root)
        registry: Registry[Any] = Registry()
        for path in (
            *sorted(self._root.glob("*.schema.json")),
            *sorted((self._root / "gate3").glob("*.schema.json")),
        ):
            schema = self._load(path)
            registry = registry.with_resource(
                str(schema["$id"]),
                Resource.from_contents(schema),
            )
        self._registry = registry

    @classmethod
    def repository_default(cls) -> ContextContractValidator:
        return cls(Path(__file__).resolve().parents[3] / "contracts")

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise ContractValidationError(f"required contract is missing: {path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ContractValidationError(f"contract is not an object: {path.name}")
        return value

    def validate(self, kind: RecordKind, payload: dict[str, Any]) -> None:
        schema = self._load(self._root / self._FILES[kind])
        try:
            Draft202012Validator(
                schema,
                registry=self._registry,
                format_checker=FormatChecker(),
            ).validate(payload)
        except JSONSchemaValidationError as exc:
            raise ContractValidationError(
                f"payload violates {schema.get('title', kind.value)}"
            ) from exc
