from __future__ import annotations

import os
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from services.agent_runtime.agents import (
    AgentInput,
    DeterministicLocalProvider,
    ProviderOutput,
)
from services.agent_runtime.local_runtime import (
    DeepSeekAssembly,
    LocalRuntimeDisabled,
    LocalRuntimeError,
    compose_local_provider,
)


class _Ledger:
    def monthly_cost_usd(self) -> Decimal:
        return Decimal("0")


class _Vault:
    pass


class _Provider:
    provider_version = "deepseek-test-v1"
    tool_version = "no-tools-v1"

    def generate(self, request: AgentInput) -> ProviderOutput:
        raise AssertionError("composition must not invoke the provider")


def _manifest() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "gbos.localhost",
        "production_go": False,
        "local_pilot_go": False,
        "local_pilot_status": "disabled",
        "capabilities": {
            "kingdee": False,
            "cloud_server": False,
            "cloud_business_storage": False,
            "external_send": False,
            "formal_business_commands": False,
        },
        "deepseek": {
            "enabled": False,
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "keychain_ref": None,
            "kill_switch": True,
            "thinking_default": "disabled",
            "max_input_tokens": 32768,
            "max_output_tokens": 4096,
            "soft_limit_usd": 50,
            "hard_limit_usd": 100,
        },
    }


def test_local_provider_composition_is_disabled_by_default() -> None:
    with pytest.raises(LocalRuntimeDisabled, match="disabled"):
        compose_local_provider(_manifest())


def test_deterministic_provider_requires_explicit_synthetic_e2e_mode() -> None:
    with pytest.raises(LocalRuntimeError, match="synthetic"):
        compose_local_provider(
            _manifest(),
            runtime_enabled=True,
            provider_mode="deterministic",
        )

    provider = compose_local_provider(
        _manifest(),
        runtime_enabled=True,
        provider_mode="deterministic",
        synthetic_e2e=True,
    )
    assert isinstance(provider, DeterministicLocalProvider)
    assert provider.provider_version == "deterministic-local-v1"


def _enabled_manifest() -> dict[str, Any]:
    manifest = deepcopy(_manifest())
    manifest["local_pilot_go"] = True
    manifest["local_pilot_status"] = "ready"
    manifest["deepseek"].update(
        enabled=True,
        keychain_ref="keychain://com.esan.gbos.local-pilot/deepseek",
        kill_switch=False,
    )
    return manifest


def test_deepseek_composition_requires_every_fail_closed_dependency(tmp_path: Path) -> None:
    key_file = tmp_path / "deepseek_api_key"
    key_file.write_text("secret-key\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    baseline = {
        "runtime_enabled": True,
        "provider_mode": "deepseek",
        "model_kill_switch": False,
        "key_file": key_file,
        "budget_ledger": _Ledger(),
        "tokenizer_vault": _Vault(),
        "controlled_egress": True,
        "deepseek_factory": lambda assembly: _Provider(),
    }
    omissions = (
        {"model_kill_switch": True},
        {"key_file": tmp_path / "missing"},
        {"budget_ledger": None},
        {"tokenizer_vault": None},
        {"controlled_egress": False},
        {"deepseek_factory": None},
    )
    for omission in omissions:
        with pytest.raises((LocalRuntimeDisabled, LocalRuntimeError)):
            compose_local_provider(_enabled_manifest(), **{**baseline, **omission})


def test_deepseek_composition_accepts_only_exact_manifest_and_never_calls_network(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "deepseek_api_key"
    key_file.write_text("secret-key\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    calls: list[DeepSeekAssembly] = []

    def factory(assembly: DeepSeekAssembly) -> object:
        calls.append(assembly)
        return _Provider()

    provider = compose_local_provider(
        _enabled_manifest(),
        runtime_enabled=True,
        provider_mode="deepseek",
        model_kill_switch=False,
        key_file=key_file,
        budget_ledger=_Ledger(),
        tokenizer_vault=_Vault(),
        controlled_egress=True,
        deepseek_factory=factory,
    )

    assert provider is not None
    assert len(calls) == 1
    assert calls[0].base_url == "https://api.deepseek.com"
    assert calls[0].model == "deepseek-v4-flash"
    assert calls[0].api_key == "secret-key"
    assert "secret-key" not in repr(calls[0])

    wrong = _enabled_manifest()
    wrong["deepseek"]["model"] = "deepseek-chat"
    with pytest.raises(LocalRuntimeError, match="exact"):
        compose_local_provider(
            wrong,
            runtime_enabled=True,
            provider_mode="deepseek",
            model_kill_switch=False,
            key_file=key_file,
            budget_ledger=_Ledger(),
            tokenizer_vault=_Vault(),
            controlled_egress=True,
            deepseek_factory=factory,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: manifest.update(production_go=True),
        lambda manifest: manifest["capabilities"].update(external_send=True),
        lambda manifest: manifest["capabilities"].update(formal_business_commands=True),
        lambda manifest: manifest["capabilities"].update(kingdee=True),
    ],
)
def test_deepseek_composition_rejects_production_or_business_effect_capabilities(
    tmp_path: Path,
    mutation: object,
) -> None:
    key_file = tmp_path / "deepseek_api_key"
    key_file.write_text("secret-key\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    manifest = _enabled_manifest()
    mutation(manifest)  # type: ignore[operator]

    with pytest.raises(LocalRuntimeError, match="capabilit|production"):
        compose_local_provider(
            manifest,
            runtime_enabled=True,
            provider_mode="deepseek",
            model_kill_switch=False,
            key_file=key_file,
            budget_ledger=_Ledger(),
            tokenizer_vault=_Vault(),
            controlled_egress=True,
            deepseek_factory=lambda assembly: _Provider(),
        )


def test_deepseek_factory_cannot_compose_a_tool_capable_provider(tmp_path: Path) -> None:
    class ToolProvider(_Provider):
        tool_version = "tools-enabled-v1"

    key_file = tmp_path / "deepseek_api_key"
    key_file.write_text("secret-key\n", encoding="utf-8")
    os.chmod(key_file, 0o600)

    with pytest.raises(LocalRuntimeError, match="no-tools"):
        compose_local_provider(
            _enabled_manifest(),
            runtime_enabled=True,
            provider_mode="deepseek",
            model_kill_switch=False,
            key_file=key_file,
            budget_ledger=_Ledger(),
            tokenizer_vault=_Vault(),
            controlled_egress=True,
            deepseek_factory=lambda assembly: ToolProvider(),
        )
