from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from services.local_pilot_runtime.deployment_secret_preflight import (
    DEFAULT_SECRET_ROOT,
    PREFLIGHT_ERROR_BINDING,
    PREFLIGHT_ERROR_CONTRACT,
    PREFLIGHT_ERROR_LOCAL_INPUT,
    PREFLIGHT_ERROR_MOUNT,
    PREFLIGHT_ERROR_ROOT,
    PREFLIGHT_OK,
    DeploymentSecretPreflightError,
    preflight_then_start,
    run_deployment_secret_preflight,
)
from services.local_pilot_runtime.secret_provider import SecretBytes, SecretSpec, SecretText

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "contracts" / "examples" / "gate6" / "deployment-secret-projection-valid.json"


def _example() -> dict[str, Any]:
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _contract(tmp_path: Path, value: Mapping[str, Any] | None = None) -> Path:
    path = tmp_path / "deployment-secret-projection.json"
    path.write_text(json.dumps(value or _example()), encoding="utf-8")
    return path


def _mount_required(root: Path, contract: Mapping[str, Any]) -> None:
    root.mkdir(mode=0o700)
    for item in contract["projections"]:
        if item["required"] is not True:
            continue
        if item["kind"] == "closed_json":
            payload = b"{}"
        elif item["kind"] == "bytes":
            payload = b"x" * item["exact_bytes"]
        else:
            payload = b"x" * item["minimum_bytes"]
        path = root / Path(item["target_filename"]).name
        path.write_bytes(payload)
        path.chmod(0o600)


class _RecordingProvider:
    def __init__(self, events: list[str], specs: Sequence[SecretSpec]) -> None:
        self._events = events
        self._specs = {spec.name: spec for spec in specs}

    def read_text(self, name: str) -> SecretText | None:
        self._events.append(f"read:{name}")
        return None if not self._specs[name].required else SecretText("x")

    def read_bytes(self, name: str) -> SecretBytes | None:
        self._events.append(f"read:{name}")
        return None if not self._specs[name].required else SecretBytes(b"x" * 32)

    def read_json_bytes(self, name: str) -> SecretBytes | None:
        self._events.append(f"read:{name}")
        return None if not self._specs[name].required else SecretBytes(b"{}")


def _recording_factory(
    events: list[str], captured: dict[str, object]
) -> Callable[[Path, Sequence[SecretSpec]], _RecordingProvider]:
    def factory(root: Path, specs: Sequence[SecretSpec]) -> _RecordingProvider:
        events.append("provider")
        captured["root"] = root
        captured["specs"] = tuple(specs)
        return _RecordingProvider(events, specs)

    return factory


def test_preflight_validates_binding_builds_closed_specs_and_reads_all_catalog_entries(
    tmp_path: Path,
) -> None:
    contract = _example()
    events: list[str] = []
    captured: dict[str, object] = {}
    secret_root = tmp_path / "mounted-secrets"

    result = run_deployment_secret_preflight(
        _contract(tmp_path, contract),
        site_id="gbos-site-001",
        environment="preproduction",
        secret_root=secret_root,
        environ={},
        provider_factory=_recording_factory(events, captured),
    )

    assert result == PREFLIGHT_OK
    assert captured["root"] == secret_root
    specs = captured["specs"]
    assert isinstance(specs, tuple)
    assert len(specs) == 26
    assert {spec.name for spec in specs} == {
        item["logical_name"] for item in contract["projections"]
    }
    for spec, item in zip(specs, contract["projections"], strict=True):
        assert spec == SecretSpec(
            name=item["logical_name"],
            filename=Path(item["target_filename"]).name,
            kind=item["kind"],
            minimum_bytes=item["minimum_bytes"],
            maximum_bytes=item["maximum_bytes"],
            exact_bytes=item.get("exact_bytes"),
            required=item["required"],
        )
    assert events[0] == "provider"
    assert events[1:] == [f"read:{item['logical_name']}" for item in contract["projections"]]


def test_preflight_reads_real_required_mounts_and_allows_absent_optional_mounts(
    tmp_path: Path,
) -> None:
    contract = _example()
    secret_root = tmp_path / "mounted-secrets"
    _mount_required(secret_root, contract)

    assert (
        run_deployment_secret_preflight(
            _contract(tmp_path, contract),
            site_id="gbos-site-001",
            environment="preproduction",
            secret_root=secret_root,
            environ={},
        )
        == PREFLIGHT_OK
    )


@pytest.mark.parametrize(
    ("site_id", "environment"),
    [
        ("other-site", "preproduction"),
        ("gbos-site-001", "production"),
    ],
)
def test_binding_mismatch_fails_before_provider_factory(
    tmp_path: Path,
    site_id: str,
    environment: str,
) -> None:
    events: list[str] = []

    with pytest.raises(DeploymentSecretPreflightError) as caught:
        run_deployment_secret_preflight(
            _contract(tmp_path),
            site_id=site_id,
            environment=environment,
            secret_root=tmp_path / "mounted-secrets",
            environ={},
            provider_factory=_recording_factory(events, {}),
        )

    assert caught.value.code == PREFLIGHT_ERROR_BINDING
    assert str(caught.value) == PREFLIGHT_ERROR_BINDING
    assert events == []


@pytest.mark.parametrize(
    ("mutation", "marker"),
    [
        (
            {"provider_payload": {"secret": "distinctive-provider-value"}},
            "distinctive-provider-value",
        ),
        ({"keychain_ref": "keychain://gbos/password"}, "keychain://gbos/password"),
        ({"uri": "keychain://gbos/password"}, "keychain://gbos/password"),
        ({"tool": "/usr/bin/security find-generic-password"}, "find-generic-password"),
        ({"value": "distinctive-plaintext-value"}, "distinctive-plaintext-value"),
    ],
)
def test_local_only_contract_inputs_are_rejected_before_provider_without_disclosure(
    tmp_path: Path,
    mutation: dict[str, object],
    marker: str,
) -> None:
    contract = copy.deepcopy(_example())
    contract["projections"][0].update(mutation)
    events: list[str] = []

    with pytest.raises(DeploymentSecretPreflightError) as caught:
        run_deployment_secret_preflight(
            _contract(tmp_path, contract),
            site_id="gbos-site-001",
            environment="preproduction",
            secret_root=tmp_path / "mounted-secrets",
            environ={},
            provider_factory=_recording_factory(events, {}),
        )

    assert caught.value.code == PREFLIGHT_ERROR_LOCAL_INPUT
    assert marker not in f"{caught.value!s} {caught.value!r}"
    assert events == []


@pytest.mark.parametrize(
    "environment",
    [
        {"POSTGRES_PASSWORD": "distinctive-password"},
        {"GBOS_AGENT_API_TOKEN": "distinctive-token"},
        {"DEEPSEEK_API_KEY": "distinctive-api-key"},
        {"GBOS_AUTH_REF": "keychain://gbos/password"},
        {"GBOS_SECRET_TOOL": "/usr/bin/security find-generic-password"},
        {"GBOS_PROVIDER_PAYLOAD": "distinctive-provider-payload"},
    ],
)
def test_local_only_environment_fails_before_provider_without_disclosure(
    tmp_path: Path,
    environment: dict[str, str],
) -> None:
    events: list[str] = []

    with pytest.raises(DeploymentSecretPreflightError) as caught:
        run_deployment_secret_preflight(
            _contract(tmp_path),
            site_id="gbos-site-001",
            environment="preproduction",
            secret_root=tmp_path / "mounted-secrets",
            environ=environment,
            provider_factory=_recording_factory(events, {}),
        )

    assert caught.value.code == PREFLIGHT_ERROR_LOCAL_INPUT
    assert not any(value in f"{caught.value!s} {caught.value!r}" for value in environment.values())
    assert events == []


def test_contract_schema_violation_fails_before_provider(tmp_path: Path) -> None:
    contract = copy.deepcopy(_example())
    contract["projections"].pop()
    events: list[str] = []

    with pytest.raises(DeploymentSecretPreflightError) as caught:
        run_deployment_secret_preflight(
            _contract(tmp_path, contract),
            site_id="gbos-site-001",
            environment="preproduction",
            secret_root=tmp_path / "mounted-secrets",
            environ={},
            provider_factory=_recording_factory(events, {}),
        )

    assert caught.value.code == PREFLIGHT_ERROR_CONTRACT
    assert events == []


@pytest.mark.parametrize(
    "root",
    [
        ROOT / "secrets",
        ROOT / "tests" / "secret-fixtures",
        Path("relative-secrets"),
    ],
)
def test_repository_contained_or_relative_secret_roots_fail_before_provider(
    tmp_path: Path,
    root: Path,
) -> None:
    events: list[str] = []

    with pytest.raises(DeploymentSecretPreflightError) as caught:
        run_deployment_secret_preflight(
            _contract(tmp_path),
            site_id="gbos-site-001",
            environment="preproduction",
            secret_root=root,
            environ={},
            provider_factory=_recording_factory(events, {}),
        )

    assert caught.value.code == PREFLIGHT_ERROR_ROOT
    assert str(root) not in f"{caught.value!s} {caught.value!r}"
    assert events == []


def test_mount_failure_returns_only_stable_code_without_secret_or_path(tmp_path: Path) -> None:
    secret_root = tmp_path / "mounted-secrets"
    secret_root.mkdir(mode=0o700)
    marker = "distinctive-secret-marker"
    path = secret_root / "postgres_password"
    path.write_text(marker, encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(DeploymentSecretPreflightError) as caught:
        run_deployment_secret_preflight(
            _contract(tmp_path),
            site_id="gbos-site-001",
            environment="preproduction",
            secret_root=secret_root,
            environ={},
        )

    assert caught.value.code == PREFLIGHT_ERROR_MOUNT
    rendered = f"{caught.value!s} {caught.value!r}"
    assert rendered.count(PREFLIGHT_ERROR_MOUNT) >= 1
    assert marker not in rendered
    assert str(path) not in rendered


@pytest.mark.parametrize(
    "factory_name",
    [
        "database-connector",
        "application-server",
        "frappe-client",
        "network-factory",
        "provider-client-factory",
    ],
)
def test_preflight_completes_before_any_runtime_factory_is_invoked(
    tmp_path: Path,
    factory_name: str,
) -> None:
    events: list[str] = []
    contract = _example()

    def runtime_factory() -> str:
        events.append(factory_name)
        return "started"

    result = preflight_then_start(
        runtime_factory,
        contract_path=_contract(tmp_path, contract),
        site_id="gbos-site-001",
        environment="preproduction",
        secret_root=tmp_path / "mounted-secrets",
        environ={},
        provider_factory=_recording_factory(events, {}),
    )

    assert result == "started"
    assert events[-1] == factory_name
    assert all(event.startswith("read:") for event in events[1:-1])


@pytest.mark.parametrize(
    "factory_name",
    [
        "database-connector",
        "application-server",
        "frappe-client",
        "network-factory",
        "provider-client-factory",
    ],
)
def test_failed_preflight_never_invokes_runtime_factory(
    tmp_path: Path,
    factory_name: str,
) -> None:
    events: list[str] = []

    def runtime_factory() -> None:
        events.append(factory_name)

    with pytest.raises(DeploymentSecretPreflightError):
        preflight_then_start(
            runtime_factory,
            contract_path=_contract(tmp_path),
            site_id="wrong-site",
            environment="preproduction",
            secret_root=tmp_path / "mounted-secrets",
            environ={},
            provider_factory=_recording_factory(events, {}),
        )

    assert events == []


def test_default_secret_root_is_the_platform_mount() -> None:
    assert Path("/run/secrets") == DEFAULT_SECRET_ROOT
