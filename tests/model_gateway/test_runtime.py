from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.agent_runtime.invocations import (
    InMemoryModelInvocationRepository,
    PostgresModelInvocationRepository,
)
from services.agent_runtime.local_runtime import DeepSeekAssembly
from services.model_gateway.deepseek import BudgetHardStop, InMemoryUsageLedger
from services.model_gateway.observation_provider import DeepSeekObservationProvider
from services.model_gateway.provider import DeepSeekAgentProvider
from services.model_gateway.runtime import (
    CONSERVATIVE_TOKEN_COUNTER_VERSION,
    DEEPSEEK_PRICE_CATALOG_VERSION,
    TOKEN_PROTOCOL_OVERHEAD,
    ConservativeTokenCounter,
    DeepSeekV4FlashPriceCalculator,
    PostgresMonthlyUsageLedger,
    create_deepseek_agent_provider_factory,
    create_deepseek_observation_provider,
)
from services.model_gateway.tokenization import (
    EncryptedFileMappingVault,
    InMemoryMappingVault,
)

NOW = datetime(2026, 8, 8, 2, 0, tzinfo=UTC)


def _secret_file(path: Path, value: bytes) -> Path:
    path.write_bytes(value)
    os.chmod(path, 0o600)
    return path


def test_conservative_counter_is_a_versioned_utf8_byte_upper_bound() -> None:
    counter = ConservativeTokenCounter()

    assert counter.version == CONSERVATIVE_TOKEN_COUNTER_VERSION
    assert counter.count("") == TOKEN_PROTOCOL_OVERHEAD
    assert counter.count("plain ASCII") == len(b"plain ASCII") + TOKEN_PROTOCOL_OVERHEAD
    assert counter.count("中文🙂") == len("中文🙂".encode()) + TOKEN_PROTOCOL_OVERHEAD


def test_fixed_price_catalog_uses_cache_miss_input_and_output_rates() -> None:
    calculator = DeepSeekV4FlashPriceCalculator()

    assert calculator.catalog_version == DEEPSEEK_PRICE_CATALOG_VERSION
    assert calculator.calculate(
        model="deepseek-v4-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    ) == Decimal("0.42")
    with pytest.raises(ValueError, match="model"):
        calculator.calculate(
            model="unverified-model",
            input_tokens=1,
            output_tokens=1,
        )


def test_aes256gcm_vault_requires_exact_private_regular_32_byte_key(
    tmp_path: Path,
) -> None:
    key = _secret_file(tmp_path / "master.key", b"k" * 32)
    EncryptedFileMappingVault.from_key_file(root=tmp_path / "vault", key_file=key)

    os.chmod(key, 0o640)
    with pytest.raises(ValueError, match="permissions"):
        EncryptedFileMappingVault.from_key_file(root=tmp_path / "broad", key_file=key)
    os.chmod(key, 0o600)

    short = _secret_file(tmp_path / "short.key", b"k" * 31)
    with pytest.raises(ValueError, match="32 bytes"):
        EncryptedFileMappingVault.from_key_file(root=tmp_path / "short", key_file=short)

    symlink = tmp_path / "linked.key"
    symlink.symlink_to(key)
    with pytest.raises(ValueError, match="regular"):
        EncryptedFileMappingVault.from_key_file(root=tmp_path / "linked", key_file=symlink)


def test_aes256gcm_vault_uses_random_nonce_authenticated_scope_and_metadata(
    tmp_path: Path,
) -> None:
    key = _secret_file(tmp_path / "master.key", b"k" * 32)
    first = EncryptedFileMappingVault.from_key_file(
        root=tmp_path / "first",
        key_file=key,
        clock=lambda: NOW,
    )
    second = EncryptedFileMappingVault.from_key_file(
        root=tmp_path / "second",
        key_file=key,
        clock=lambda: NOW,
    )
    record_id = "a" * 64
    mapping = {"<EMAIL_deadbeef>": "private@example.com"}

    first_ref = first.store(
        mapping,
        record_id=record_id,
        site_id="gbos.localhost",
        purpose="sales_follow_up",
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )
    second.store(
        mapping,
        record_id=record_id,
        site_id="gbos.localhost",
        purpose="sales_follow_up",
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )

    first_bytes = (tmp_path / "first" / f"{record_id}.json").read_bytes()
    second_bytes = (tmp_path / "second" / f"{record_id}.json").read_bytes()
    assert first_bytes != second_bytes
    assert b"private@example.com" not in first_bytes
    assert b"gbos.localhost" not in first_bytes
    assert (
        first.load(
            first_ref,
            site_id="gbos.localhost",
            purpose="sales_follow_up",
        )
        == mapping
    )
    with pytest.raises(PermissionError):
        first.load(
            first_ref,
            site_id="other.localhost",
            purpose="sales_follow_up",
        )
    assert "private@example.com" not in repr(first)
    assert (b"k" * 32).hex() not in repr(first)

    envelope_path = tmp_path / "first" / f"{record_id}.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["expires_at"] = "2026-08-07T02:00:00Z"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError, match="authenticated"):
        first.cleanup_expired(now=NOW + timedelta(days=31))
    assert envelope_path.exists()


def test_vault_cleanup_authenticates_then_removes_expired_envelopes(tmp_path: Path) -> None:
    key = _secret_file(tmp_path / "master.key", b"k" * 32)
    vault = EncryptedFileMappingVault.from_key_file(
        root=tmp_path / "vault",
        key_file=key,
        clock=lambda: NOW,
    )
    expired_id = "a" * 64
    live_id = "b" * 64
    for record_id, expiry in (
        (expired_id, NOW + timedelta(days=1)),
        (live_id, NOW + timedelta(days=30)),
    ):
        vault.store(
            {"<ENTITY_deadbeef>": "private"},
            record_id=record_id,
            site_id="gbos.localhost",
            purpose="sales_follow_up",
            created_at=NOW,
            expires_at=expiry,
        )

    assert vault.cleanup_expired(now=NOW + timedelta(days=2)) == 1
    assert not (tmp_path / "vault" / f"{expired_id}.json").exists()
    assert (tmp_path / "vault" / f"{live_id}.json").exists()
    assert not (tmp_path / "vault" / "index.json").exists()


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0)

    def fetchall(self) -> list[tuple[Any, ...]]:
        raise AssertionError("fetchall is not used")


class _Transaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None


class _Connection:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.cursor_instance = _Cursor(rows)

    def transaction(self) -> _Transaction:
        return _Transaction()

    def cursor(self) -> _Cursor:
        return self.cursor_instance


def test_postgres_ledger_reserves_under_site_month_lock_before_network() -> None:
    connection = _Connection(
        [
            None,
            (0,),
            (Decimal("99.90"),),
            ("reserved", Decimal("0.10"), Decimal("0.10")),
        ]
    )
    ledger = PostgresMonthlyUsageLedger(
        connection=connection,
        site_id="gbos.localhost",
        clock=lambda: NOW,
    )

    reservation = ledger.reserve(
        reservation_id="model-call-1",
        amount_usd=Decimal("0.10"),
        price_catalog_version=DEEPSEEK_PRICE_CATALOG_VERSION,
        token_counter_version=CONSERVATIVE_TOKEN_COUNTER_VERSION,
    )

    assert reservation.amount_usd == Decimal("0.10")
    assert reservation.budget_month == date(2026, 8, 1)
    sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed).lower()
    assert "set_config('app.site_id'" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "model_budget_reservations" in sql
    assert "model_invocations" in sql


def test_postgres_ledger_fails_closed_on_unknown_durable_cost() -> None:
    connection = _Connection([None, (1,)])
    ledger = PostgresMonthlyUsageLedger(
        connection=connection,
        site_id="gbos.localhost",
        clock=lambda: NOW,
    )

    with pytest.raises(BudgetHardStop, match="unknown"):
        ledger.reserve(
            reservation_id="model-call-1",
            amount_usd=Decimal("0.01"),
            price_catalog_version=DEEPSEEK_PRICE_CATALOG_VERSION,
            token_counter_version=CONSERVATIVE_TOKEN_COUNTER_VERSION,
        )


def test_budget_migration_is_rls_locked_content_free_and_idempotent() -> None:
    migration = (
        (
            Path(__file__).parents[2]
            / "services"
            / "agent_runtime"
            / "migrations"
            / "005_local_pilot_model_budget.sql"
        )
        .read_text(encoding="utf-8")
        .lower()
    )

    assert "create table if not exists agent_runtime.model_budget_reservations" in migration
    assert "primary key (site_id, reservation_id)" in migration
    assert "force row level security" in migration
    assert "current_setting('app.site_id', true)" in migration
    assert "price_catalog_version" in migration
    assert "token_counter_version" in migration
    for forbidden in ("prompt_text", "response_text", "api_key", "mapping", "email", "phone"):
        assert forbidden not in migration


class _NoNetworkTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("factory construction must not perform network I/O")


def test_factory_builds_exact_default_off_no_tools_provider(tmp_path: Path) -> None:
    hmac_key_file = _secret_file(tmp_path / "hmac.key", b"h" * 32)
    master_key_file = _secret_file(tmp_path / "master.key", b"m" * 32)
    ledger = PostgresMonthlyUsageLedger(
        connection=_Connection([]),
        site_id="gbos.localhost",
        clock=lambda: NOW,
    )
    factory = create_deepseek_agent_provider_factory(
        tokenizer_hmac_key_file=hmac_key_file,
        phrase_resolver=lambda _: (),
        audit_repository=PostgresModelInvocationRepository(_Connection([])),
        transport_factory=_NoNetworkTransport,
        clock=lambda: NOW,
    )
    assembly = DeepSeekAssembly(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        api_key="test-api-key",
        budget_ledger=ledger,
        tokenizer_vault=EncryptedFileMappingVault.from_key_file(
            root=tmp_path / "vault",
            key_file=master_key_file,
        ),
    )

    provider = factory(assembly)

    assert isinstance(provider, DeepSeekAgentProvider)
    assert provider.tool_version == "no-tools-v1"
    assert "test-api-key" not in repr(provider)


def test_observation_factory_uses_exact_model_without_retokenization_or_tools(
    tmp_path: Path,
) -> None:
    master_key_file = _secret_file(tmp_path / "master.key", b"m" * 32)
    ledger = PostgresMonthlyUsageLedger(
        connection=_Connection([]),
        site_id="gbos.localhost",
        clock=lambda: NOW,
    )
    assembly = DeepSeekAssembly(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        api_key="observation-api-key",
        budget_ledger=ledger,
        tokenizer_vault=EncryptedFileMappingVault.from_key_file(
            root=tmp_path / "vault",
            key_file=master_key_file,
        ),
    )

    provider = create_deepseek_observation_provider(
        assembly=assembly,
        audit_repository=PostgresModelInvocationRepository(_Connection([])),
        transport_factory=_NoNetworkTransport,
        network_enabled=False,
        clock=lambda: NOW,
    )

    assert isinstance(provider, DeepSeekObservationProvider)
    assert "observation-api-key" not in repr(provider)
    assert not hasattr(provider, "_tokenizer")
    assert not hasattr(provider, "_phrase_resolver")


def test_factory_rejects_wrong_endpoint_model_or_unsafe_hmac_key(tmp_path: Path) -> None:
    key_file = _secret_file(tmp_path / "hmac.key", b"h" * 32)
    master_key_file = _secret_file(tmp_path / "master.key", b"m" * 32)
    factory = create_deepseek_agent_provider_factory(
        tokenizer_hmac_key_file=key_file,
        phrase_resolver=lambda _: (),
        audit_repository=PostgresModelInvocationRepository(_Connection([])),
        transport_factory=_NoNetworkTransport,
    )
    baseline = {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key": "test-api-key",
        "budget_ledger": PostgresMonthlyUsageLedger(
            connection=_Connection([]),
            site_id="gbos.localhost",
            clock=lambda: NOW,
        ),
        "tokenizer_vault": EncryptedFileMappingVault.from_key_file(
            root=tmp_path / "vault",
            key_file=master_key_file,
        ),
    }
    for mutation in (
        {"base_url": "https://example.invalid"},
        {"model": "deepseek-chat"},
    ):
        with pytest.raises(ValueError, match="exact"):
            factory(DeepSeekAssembly(**{**baseline, **mutation}))

    with pytest.raises(ValueError, match="encrypted"):
        factory(DeepSeekAssembly(**{**baseline, "tokenizer_vault": InMemoryMappingVault()}))
    with pytest.raises(ValueError, match="durable"):
        factory(DeepSeekAssembly(**{**baseline, "budget_ledger": InMemoryUsageLedger()}))
    with pytest.raises(ValueError, match="audit"):
        create_deepseek_agent_provider_factory(
            tokenizer_hmac_key_file=key_file,
            phrase_resolver=lambda _: (),
            audit_repository=InMemoryModelInvocationRepository(),
            transport_factory=_NoNetworkTransport,
        )

    os.chmod(key_file, 0o644)
    with pytest.raises(ValueError, match="permissions"):
        create_deepseek_agent_provider_factory(
            tokenizer_hmac_key_file=key_file,
            phrase_resolver=lambda _: (),
            audit_repository=InMemoryModelInvocationRepository(),
            transport_factory=_NoNetworkTransport,
        )
