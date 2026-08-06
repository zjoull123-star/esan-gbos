from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from referencing import Registry, Resource


class ContractValidationError(ValueError):
    """A payload failed a frozen Gate 3 wire contract."""


class ContractValidator:
    def __init__(self, contracts_root: Path) -> None:
        self._root = Path(contracts_root)
        registry: Registry[Any] = Registry()
        for path in sorted(self._root.glob("*.schema.json")):
            schema = self._load(path)
            registry = registry.with_resource(
                str(schema["$id"]),
                Resource.from_contents(schema),
            )
        gate3 = self._root / "gate3"
        for path in sorted(gate3.glob("*.schema.json")):
            schema = self._load(path)
            registry = registry.with_resource(
                str(schema["$id"]),
                Resource.from_contents(schema),
            )
        self._registry = registry

    @classmethod
    def repository_default(cls) -> ContractValidator:
        repository_root = Path(__file__).resolve().parents[3]
        return cls(repository_root / "contracts")

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise ContractValidationError(f"required contract is missing: {path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ContractValidationError(f"contract is not an object: {path.name}")
        return value

    def validate_gate3(self, filename: str, payload: dict[str, Any]) -> None:
        if "/" in filename or "\\" in filename or not filename.endswith(".schema.json"):
            raise ContractValidationError("invalid contract filename")
        schema = self._load(self._root / "gate3" / filename)
        try:
            Draft202012Validator(
                schema,
                registry=self._registry,
                format_checker=FormatChecker(),
            ).validate(payload)
        except JSONSchemaValidationError as exc:
            raise ContractValidationError(
                f"payload violates {schema.get('title', filename)}"
            ) from exc
