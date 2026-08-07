"""Production composition primitives for the fixed DeepSeek local runtime."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx

from services.agent_runtime.agents import AgentInput
from services.agent_runtime.invocations import (
    ModelInvocationRepository,
    PostgresModelInvocationRepository,
)
from services.agent_runtime.local_runtime import DeepSeekAssembly
from services.agent_runtime.postgres import Connection, Cursor

from .deepseek import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    HARD_MONTHLY_LIMIT_USD,
    BudgetHardStop,
    BudgetReservation,
    CostSnapshot,
    DeepSeekAdapter,
    UsageLedger,
    UsageSnapshot,
)
from .observation_provider import DeepSeekObservationProvider
from .provider import DeepSeekAgentProvider
from .tokenization import EncryptedFileMappingVault, StableTokenizer

CONSERVATIVE_TOKEN_COUNTER_VERSION = "deepseek-utf8-byte-plus-1024-upper-bound-v1"
TOKEN_PROTOCOL_OVERHEAD = 1_024
DEEPSEEK_PRICE_CATALOG_VERSION = "deepseek-v4-flash-official-2026-04-24"
DEEPSEEK_CACHE_MISS_INPUT_USD_PER_MILLION = Decimal("0.14")
DEEPSEEK_OUTPUT_USD_PER_MILLION = Decimal("0.28")
_MILLION = Decimal("1000000")
_SITE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ConservativeTokenCounter:
    """UTF-8 bytes plus fixed headroom for the gateway's two-message protocol."""

    version = CONSERVATIVE_TOKEN_COUNTER_VERSION

    def count(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("token counter input must be text")
        return len(text.encode("utf-8")) + TOKEN_PROTOCOL_OVERHEAD


class DeepSeekV4FlashPriceCalculator:
    """Frozen official V4 Flash catalog; input always uses cache-miss pricing."""

    catalog_version = DEEPSEEK_PRICE_CATALOG_VERSION

    def calculate(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        if model != DEEPSEEK_MODEL:
            raise ValueError("price catalog does not contain the requested model")
        for value in (input_tokens, output_tokens):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("token counts must be non-negative integers")
        return (
            Decimal(input_tokens) * DEEPSEEK_CACHE_MISS_INPUT_USD_PER_MILLION
            + Decimal(output_tokens) * DEEPSEEK_OUTPUT_USD_PER_MILLION
        ) / _MILLION


class PostgresMonthlyUsageLedger:
    """Cross-process per-site monthly budget reservations over durable audit data."""

    def __init__(
        self,
        *,
        connection: Connection,
        site_id: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if _SITE_PATTERN.fullmatch(site_id) is None:
            raise ValueError("site_id has an invalid format")
        self._connection = connection
        self._site_id = site_id
        self._clock = clock

    def monthly_cost_usd(self) -> Decimal:
        month = self._month_start()
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor)
            self._fail_on_unknown_cost(cursor, month)
            return self._charged_total(cursor, month)

    def reserve(
        self,
        *,
        reservation_id: str,
        amount_usd: Decimal,
        price_catalog_version: str,
        token_counter_version: str,
    ) -> BudgetReservation:
        _validate_reservation_values(
            reservation_id=reservation_id,
            amount_usd=amount_usd,
            price_catalog_version=price_catalog_version,
            token_counter_version=token_counter_version,
        )
        month = self._month_start()
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor)
            self._lock_month(cursor, month)
            existing = self._reservation_row(cursor, reservation_id)
            if existing is not None:
                self._validate_existing(
                    existing,
                    reservation_id=reservation_id,
                    amount_usd=amount_usd,
                    price_catalog_version=price_catalog_version,
                    token_counter_version=token_counter_version,
                    month=month,
                )
                return BudgetReservation(
                    reservation_id=reservation_id,
                    amount_usd=amount_usd,
                    price_catalog_version=price_catalog_version,
                    token_counter_version=token_counter_version,
                    budget_month=month,
                )
            self._fail_on_unknown_cost(cursor, month)
            charged = self._charged_total(cursor, month)
            if charged >= HARD_MONTHLY_LIMIT_USD or (charged + amount_usd > HARD_MONTHLY_LIMIT_USD):
                raise BudgetHardStop(
                    "monthly model budget cannot cover the worst-case call cost",
                    error_code="budget_hard_stop",
                )
            cursor.execute(
                """
                INSERT INTO agent_runtime.model_budget_reservations (
                    site_id, reservation_id, month_start, state,
                    maximum_amount_usd, charged_amount_usd,
                    price_catalog_version, token_counter_version,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, 'reserved', %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (site_id, reservation_id) DO NOTHING
                RETURNING state, maximum_amount_usd, charged_amount_usd
                """,
                (
                    self._site_id,
                    reservation_id,
                    month,
                    amount_usd,
                    amount_usd,
                    price_catalog_version,
                    token_counter_version,
                    self._now(),
                    self._now(),
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                raise BudgetHardStop(
                    "budget reservation conflict",
                    error_code="budget_hard_stop",
                )
        return BudgetReservation(
            reservation_id=reservation_id,
            amount_usd=amount_usd,
            price_catalog_version=price_catalog_version,
            token_counter_version=token_counter_version,
            budget_month=month,
        )

    def ensure_can_attempt(self, reservation: BudgetReservation) -> None:
        month = self._reservation_month(reservation)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor)
            self._lock_month(cursor, month)
            row = self._reservation_row(cursor, reservation.reservation_id)
            if row is None or row[0] != "reserved":
                raise BudgetHardStop(
                    "model budget reservation is unavailable",
                    error_code="budget_hard_stop",
                )
            self._validate_existing(
                row,
                reservation_id=reservation.reservation_id,
                amount_usd=reservation.amount_usd,
                price_catalog_version=reservation.price_catalog_version,
                token_counter_version=reservation.token_counter_version,
                month=month,
            )
            self._fail_on_unknown_cost(cursor, month)
            if self._charged_total(cursor, month) > HARD_MONTHLY_LIMIT_USD:
                raise BudgetHardStop(
                    "monthly model budget hard stop reached",
                    error_code="budget_hard_stop",
                )

    def settle(
        self,
        reservation: BudgetReservation,
        *,
        usage: UsageSnapshot,
        cost: CostSnapshot,
    ) -> None:
        del usage
        if (
            cost.status != "known"
            or cost.amount_usd is None
            or cost.catalog_version != reservation.price_catalog_version
            or cost.amount_usd > reservation.amount_usd
        ):
            raise BudgetHardStop(
                "model cost is unknown or exceeds its reservation",
                error_code="budget_hard_stop",
            )
        self._transition(
            reservation,
            state="settled",
            charged_amount=cost.amount_usd,
        )

    def consume(self, reservation: BudgetReservation) -> CostSnapshot:
        self._transition(
            reservation,
            state="consumed",
            charged_amount=reservation.amount_usd,
        )
        return CostSnapshot(
            status="known",
            amount_usd=reservation.amount_usd,
            catalog_version=reservation.price_catalog_version,
        )

    def release(self, reservation: BudgetReservation) -> None:
        month = self._reservation_month(reservation)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor)
            self._lock_month(cursor, month)
            cursor.execute(
                """
                DELETE FROM agent_runtime.model_budget_reservations
                WHERE site_id = %s
                  AND reservation_id = %s
                  AND month_start = %s
                  AND state = 'reserved'
                """,
                (self._site_id, reservation.reservation_id, month),
            )

    def _transition(
        self,
        reservation: BudgetReservation,
        *,
        state: str,
        charged_amount: Decimal,
    ) -> None:
        month = self._reservation_month(reservation)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor)
            self._lock_month(cursor, month)
            existing = self._reservation_row(cursor, reservation.reservation_id)
            if existing is None:
                raise BudgetHardStop(
                    "model budget reservation is unavailable",
                    error_code="budget_hard_stop",
                )
            self._validate_existing(
                existing,
                reservation_id=reservation.reservation_id,
                amount_usd=reservation.amount_usd,
                price_catalog_version=reservation.price_catalog_version,
                token_counter_version=reservation.token_counter_version,
                month=month,
            )
            if existing[0] == state and existing[2] == charged_amount:
                return
            if existing[0] != "reserved":
                raise BudgetHardStop(
                    "model budget reservation was already finalized",
                    error_code="budget_hard_stop",
                )
            cursor.execute(
                """
                UPDATE agent_runtime.model_budget_reservations
                SET state = %s,
                    charged_amount_usd = %s,
                    updated_at = %s
                WHERE site_id = %s
                  AND reservation_id = %s
                  AND month_start = %s
                  AND state = 'reserved'
                RETURNING state, charged_amount_usd
                """,
                (
                    state,
                    charged_amount,
                    self._now(),
                    self._site_id,
                    reservation.reservation_id,
                    month,
                ),
            )
            updated = cursor.fetchone()
            if updated != (state, charged_amount):
                raise BudgetHardStop(
                    "model budget reservation transition failed",
                    error_code="budget_hard_stop",
                )

    def _reservation_row(
        self,
        cursor: Cursor,
        reservation_id: str,
    ) -> tuple[Any, ...] | None:
        cursor.execute(
            """
            SELECT state, maximum_amount_usd, charged_amount_usd,
                   price_catalog_version, token_counter_version, month_start
            FROM agent_runtime.model_budget_reservations
            WHERE site_id = %s AND reservation_id = %s
            FOR UPDATE
            """,
            (self._site_id, reservation_id),
        )
        return cursor.fetchone()

    def _fail_on_unknown_cost(self, cursor: Cursor, month: date) -> None:
        cursor.execute(
            """
            SELECT count(*)
            FROM agent_runtime.model_invocations AS invocation
            WHERE invocation.site_id = %s
              AND invocation.started_at >= %s
              AND invocation.started_at < %s + interval '1 month'
              AND invocation.network_call_count > 0
              AND invocation.cost_status = 'unknown'
              AND NOT EXISTS (
                  SELECT 1
                  FROM agent_runtime.model_budget_reservations AS reservation
                  WHERE reservation.site_id = invocation.site_id
                    AND reservation.reservation_id = invocation.invocation_id
              )
            """,
            (self._site_id, month, month),
        )
        row = cursor.fetchone()
        if row is None or row[0] != 0:
            raise BudgetHardStop(
                "monthly model cost is unknown",
                error_code="budget_hard_stop",
            )

    def _charged_total(self, cursor: Cursor, month: date) -> Decimal:
        cursor.execute(
            """
            SELECT COALESCE(sum(charge.amount_usd), 0)
            FROM (
                SELECT invocation.cost_amount AS amount_usd
                FROM agent_runtime.model_invocations AS invocation
                WHERE invocation.site_id = %s
                  AND invocation.started_at >= %s
                  AND invocation.started_at < %s + interval '1 month'
                  AND invocation.cost_status = 'known'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_runtime.model_budget_reservations AS reservation
                      WHERE reservation.site_id = invocation.site_id
                        AND reservation.reservation_id = invocation.invocation_id
                  )
                UNION ALL
                SELECT reservation.charged_amount_usd
                FROM agent_runtime.model_budget_reservations AS reservation
                WHERE reservation.site_id = %s
                  AND reservation.month_start = %s
            ) AS charge
            """,
            (self._site_id, month, month, self._site_id, month),
        )
        row = cursor.fetchone()
        if row is None or not isinstance(row[0], Decimal) or not row[0].is_finite():
            raise BudgetHardStop(
                "monthly model cost is unknown",
                error_code="budget_hard_stop",
            )
        return row[0]

    def _lock_month(self, cursor: Cursor, month: date) -> None:
        cursor.execute(
            """
            SELECT pg_advisory_xact_lock(
                hashtextextended(%s || ':' || %s::text, 0)
            )
            """,
            (self._site_id, month),
        )

    def _set_site(self, cursor: Cursor) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (self._site_id,))

    def _validate_existing(
        self,
        row: tuple[Any, ...],
        *,
        reservation_id: str,
        amount_usd: Decimal,
        price_catalog_version: str,
        token_counter_version: str,
        month: date,
    ) -> None:
        del reservation_id
        if (
            len(row) != 6
            or row[1] != amount_usd
            or row[3] != price_catalog_version
            or row[4] != token_counter_version
            or row[5] != month
        ):
            raise BudgetHardStop(
                "budget reservation metadata conflict",
                error_code="budget_hard_stop",
            )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise BudgetHardStop(
                "budget clock must be timezone-aware",
                error_code="budget_hard_stop",
            )
        return value.astimezone(UTC)

    def _month_start(self) -> date:
        now = self._now()
        return date(now.year, now.month, 1)

    def _reservation_month(self, reservation: BudgetReservation) -> date:
        if reservation.budget_month is None:
            return self._month_start()
        if reservation.budget_month.day != 1:
            raise BudgetHardStop(
                "budget reservation month is invalid",
                error_code="budget_hard_stop",
            )
        return reservation.budget_month


def create_deepseek_agent_provider_factory(
    *,
    tokenizer_hmac_key_file: Path,
    phrase_resolver: Callable[[AgentInput], tuple[str, ...]],
    audit_repository: ModelInvocationRepository,
    transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    network_enabled: bool = False,
    clock: Callable[[], datetime] = _utc_now,
) -> Callable[[DeepSeekAssembly], DeepSeekAgentProvider]:
    """Build a no-tools factory without performing model network I/O."""

    hmac_key = _read_exact_private_key_file(tokenizer_hmac_key_file)
    if not isinstance(audit_repository, PostgresModelInvocationRepository):
        raise ValueError("DeepSeek factory requires a durable PostgreSQL audit repository")
    make_transport = transport_factory or (lambda: httpx.HTTPTransport(retries=0))

    def factory(assembly: DeepSeekAssembly) -> DeepSeekAgentProvider:
        if assembly.base_url != DEEPSEEK_BASE_URL or assembly.model != DEEPSEEK_MODEL:
            raise ValueError("DeepSeek assembly must use the exact endpoint and model")
        if (
            not isinstance(assembly.tokenizer_vault, EncryptedFileMappingVault)
            or assembly.tokenizer_vault.algorithm != "AES-256-GCM"
        ):
            raise ValueError("DeepSeek assembly requires an AES-256-GCM encrypted vault")
        if not isinstance(assembly.budget_ledger, PostgresMonthlyUsageLedger):
            raise ValueError("DeepSeek assembly requires a durable PostgreSQL budget ledger")
        ledger = cast(UsageLedger, assembly.budget_ledger)
        vault = assembly.tokenizer_vault
        tokenizer = StableTokenizer(hmac_key=hmac_key, vault=vault)
        gateway = DeepSeekAdapter(
            api_key=assembly.api_key,
            transport=make_transport(),
            token_counter=ConservativeTokenCounter(),
            price_calculator=DeepSeekV4FlashPriceCalculator(),
            usage_ledger=ledger,
            network_enabled=network_enabled,
            clock=clock,
            audit_recorder=audit_repository.append,
        )
        return DeepSeekAgentProvider(
            gateway=gateway,
            tokenizer=tokenizer,
            clock=clock,
            phrase_resolver=phrase_resolver,
        )

    return factory


def create_deepseek_observation_provider(
    *,
    assembly: DeepSeekAssembly,
    audit_repository: ModelInvocationRepository,
    transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    network_enabled: bool,
    clock: Callable[[], datetime] = _utc_now,
) -> DeepSeekObservationProvider:
    """Build the exact observation provider without re-tokenizing its bound request."""

    if (
        assembly.base_url != DEEPSEEK_BASE_URL
        or assembly.model != DEEPSEEK_MODEL
        or assembly.controlled_egress is not True
    ):
        raise ValueError("DeepSeek assembly must use the exact endpoint and model")
    if (
        not isinstance(assembly.tokenizer_vault, EncryptedFileMappingVault)
        or assembly.tokenizer_vault.algorithm != "AES-256-GCM"
    ):
        raise ValueError("DeepSeek assembly requires an AES-256-GCM encrypted vault")
    if not isinstance(assembly.budget_ledger, PostgresMonthlyUsageLedger):
        raise ValueError("DeepSeek assembly requires a durable PostgreSQL budget ledger")
    if not isinstance(audit_repository, PostgresModelInvocationRepository):
        raise ValueError("DeepSeek observation provider requires durable PostgreSQL audit")
    if not isinstance(network_enabled, bool):
        raise TypeError("network_enabled must be explicit")
    make_transport = transport_factory or (lambda: httpx.HTTPTransport(retries=0))
    gateway = DeepSeekAdapter(
        api_key=assembly.api_key,
        transport=make_transport(),
        token_counter=ConservativeTokenCounter(),
        price_calculator=DeepSeekV4FlashPriceCalculator(),
        usage_ledger=cast(UsageLedger, assembly.budget_ledger),
        network_enabled=network_enabled,
        clock=clock,
        audit_recorder=audit_repository.append,
    )
    return DeepSeekObservationProvider(gateway=gateway)


def _validate_reservation_values(
    *,
    reservation_id: str,
    amount_usd: Decimal,
    price_catalog_version: str,
    token_counter_version: str,
) -> None:
    if not reservation_id or len(reservation_id) > 256:
        raise ValueError("reservation_id must be non-empty and at most 256 characters")
    if amount_usd < 0 or not amount_usd.is_finite():
        raise ValueError("reservation amount must be finite and non-negative")
    if not price_catalog_version or len(price_catalog_version) > 80:
        raise ValueError("price catalog version is invalid")
    if not token_counter_version or len(token_counter_version) > 80:
        raise ValueError("token counter version is invalid")


def _read_exact_private_key_file(path: Path) -> bytes:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError("secret key file must be a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ValueError("secret key file must be a regular non-symlink file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("secret key file must be a regular non-symlink file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("secret key file permissions must be exactly 0600")
        value = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(value) != 32:
        raise ValueError("secret key file must contain exactly 32 bytes")
    return value


__all__ = [
    "CONSERVATIVE_TOKEN_COUNTER_VERSION",
    "DEEPSEEK_CACHE_MISS_INPUT_USD_PER_MILLION",
    "DEEPSEEK_OUTPUT_USD_PER_MILLION",
    "DEEPSEEK_PRICE_CATALOG_VERSION",
    "ConservativeTokenCounter",
    "DeepSeekV4FlashPriceCalculator",
    "PostgresMonthlyUsageLedger",
    "TOKEN_PROTOCOL_OVERHEAD",
    "create_deepseek_agent_provider_factory",
    "create_deepseek_observation_provider",
]
