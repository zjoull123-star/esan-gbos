from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from services.context.context_service.storage import ChecksumMismatch, MigrationRunner


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._row: tuple[str] | None = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.split()).lower()
        if normalized.startswith("select checksum"):
            name = str(params[0])
            checksum = self.connection.ledger.get(name)
            self._row = (checksum,) if checksum else None
        elif normalized.startswith("insert into observer.schema_migrations"):
            assert params is not None
            self.connection.ledger[str(params[0])] = str(params[1])
        elif "create table if not exists observer.schema_migrations" not in normalized:
            self.connection.applied_sql.append(sql)

    def fetchone(self) -> tuple[str] | None:
        return self._row


class FakeConnection:
    def __init__(self) -> None:
        self.ledger: dict[str, str] = {}
        self.applied_sql: list[str] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def test_migration_runner_is_idempotent_and_records_checksums(tmp_path: Path) -> None:
    migrations = tmp_path / "context" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "001_create.sql").write_text("SELECT 1;", encoding="utf-8")
    connection = FakeConnection()
    runner = MigrationRunner(connection, [migrations])

    first = runner.run()
    second = runner.run()

    assert first == ("context/001_create.sql",)
    assert second == ()
    assert connection.applied_sql.count("SELECT 1;") == 1
    assert len(connection.ledger) == 1


def test_migration_runner_rejects_changed_applied_migration(tmp_path: Path) -> None:
    migrations = tmp_path / "context" / "migrations"
    migrations.mkdir(parents=True)
    migration = migrations / "001_create.sql"
    migration.write_text("SELECT 1;", encoding="utf-8")
    connection = FakeConnection()
    MigrationRunner(connection, [migrations]).run()
    migration.write_text("SELECT 2;", encoding="utf-8")

    with pytest.raises(ChecksumMismatch):
        MigrationRunner(connection, [migrations]).run()
