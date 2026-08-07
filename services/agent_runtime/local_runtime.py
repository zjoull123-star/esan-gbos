"""Explicit, fail-closed provider composition for the local pilot."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from services.model_gateway.deepseek import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    HARD_MONTHLY_LIMIT_USD,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    SOFT_MONTHLY_LIMIT_USD,
)

from .agents import DeterministicLocalProvider, ModelProvider

ProviderMode = Literal["disabled", "deterministic", "deepseek"]


class LocalRuntimeError(ValueError):
    """A local runtime composition prerequisite was invalid or incomplete."""


class LocalRuntimeDisabled(LocalRuntimeError):
    """The default-off runtime or model kill switch blocked composition."""


class BudgetLedger(Protocol):
    def monthly_cost_usd(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class DeepSeekAssembly:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    budget_ledger: BudgetLedger = field(repr=False)
    tokenizer_vault: object = field(repr=False)
    controlled_egress: Literal[True] = True


DeepSeekFactory = Callable[[DeepSeekAssembly], object]


def validate_deepseek_manifest(
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate the exact no-business-effect DeepSeek runtime declaration."""

    return _enabled_exact_deepseek_manifest(manifest)


def compose_local_provider(
    manifest: Mapping[str, Any],
    *,
    runtime_enabled: bool = False,
    provider_mode: ProviderMode = "disabled",
    synthetic_e2e: bool = False,
    model_kill_switch: bool = True,
    key_file: Path | None = None,
    budget_ledger: BudgetLedger | None = None,
    tokenizer_vault: object | None = None,
    controlled_egress: bool = False,
    deepseek_factory: DeepSeekFactory | None = None,
) -> object:
    """Compose a provider without invoking it or opening any transport."""

    if not runtime_enabled or provider_mode == "disabled":
        raise LocalRuntimeDisabled("local Agent runtime is disabled")
    if provider_mode == "deterministic":
        if not synthetic_e2e:
            raise LocalRuntimeError("deterministic provider requires explicit synthetic E2E mode")
        return DeterministicLocalProvider()
    if provider_mode != "deepseek":
        raise LocalRuntimeError("unsupported local provider mode")
    if model_kill_switch:
        raise LocalRuntimeDisabled("model kill switch is enabled")

    deepseek = validate_deepseek_manifest(manifest)
    if key_file is None:
        raise LocalRuntimeError("DeepSeek key file is required")
    api_key = _read_secret_file(key_file)
    if budget_ledger is None:
        raise LocalRuntimeError("DeepSeek budget ledger is required")
    monthly_cost = budget_ledger.monthly_cost_usd()
    if monthly_cost < 0 or monthly_cost >= HARD_MONTHLY_LIMIT_USD:
        raise LocalRuntimeDisabled("DeepSeek budget ledger is at a hard stop")
    if tokenizer_vault is None:
        raise LocalRuntimeError("DeepSeek tokenizer vault is required")
    if not controlled_egress:
        raise LocalRuntimeDisabled("controlled egress is not enabled")
    if deepseek_factory is None:
        raise LocalRuntimeError("DeepSeek provider factory is required")

    assembly = DeepSeekAssembly(
        base_url=str(deepseek["base_url"]),
        model=str(deepseek["model"]),
        api_key=api_key,
        budget_ledger=budget_ledger,
        tokenizer_vault=tokenizer_vault,
    )
    provider = deepseek_factory(assembly)
    if not isinstance(provider, ModelProvider) or provider.tool_version != "no-tools-v1":
        raise LocalRuntimeError("DeepSeek factory must return a no-tools model provider")
    return provider


def _enabled_exact_deepseek_manifest(
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("mode") != "local_pilot"
        or manifest.get("production_go") is not False
        or manifest.get("local_pilot_go") is not True
        or manifest.get("local_pilot_status") not in {"ready", "running"}
    ):
        raise LocalRuntimeDisabled("local pilot manifest production or enablement state is unsafe")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, Mapping) or any(
        capabilities.get(name) is not False
        for name in (
            "kingdee",
            "cloud_server",
            "cloud_business_storage",
            "external_send",
            "formal_business_commands",
        )
    ):
        raise LocalRuntimeError("local pilot capability manifest is unsafe")
    value = manifest.get("deepseek")
    if not isinstance(value, Mapping):
        raise LocalRuntimeError("DeepSeek manifest section is missing")
    exact = {
        "enabled": True,
        "base_url": DEEPSEEK_BASE_URL,
        "model": DEEPSEEK_MODEL,
        "kill_switch": False,
        "thinking_default": "disabled",
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "soft_limit_usd": int(SOFT_MONTHLY_LIMIT_USD),
        "hard_limit_usd": int(HARD_MONTHLY_LIMIT_USD),
    }
    if any(value.get(key) != expected for key, expected in exact.items()):
        raise LocalRuntimeError("DeepSeek manifest does not match the exact runtime contract")
    keychain_ref = value.get("keychain_ref")
    if not isinstance(keychain_ref, str) or not keychain_ref.startswith("keychain://"):
        raise LocalRuntimeError("DeepSeek manifest requires a keychain reference")
    return value


def _read_secret_file(path: Path) -> str:
    resolved = Path(path)
    if not resolved.is_file() or resolved.is_symlink():
        raise LocalRuntimeError("DeepSeek key file is absent or unsafe")
    mode = os.stat(resolved, follow_symlinks=False).st_mode
    if mode & 0o077:
        raise LocalRuntimeError("DeepSeek key file permissions are too broad")
    if resolved.stat().st_size > 4096:
        raise LocalRuntimeError("DeepSeek key file is too large")
    value = resolved.read_text(encoding="utf-8").strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise LocalRuntimeError("DeepSeek key file is invalid")
    return value


__all__ = [
    "BudgetLedger",
    "DeepSeekAssembly",
    "LocalRuntimeDisabled",
    "LocalRuntimeError",
    "ProviderMode",
    "compose_local_provider",
    "validate_deepseek_manifest",
]
