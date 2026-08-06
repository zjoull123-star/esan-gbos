from __future__ import annotations

import json
import os
import socket
from pathlib import Path

CONTRACT_ROOT = Path("/contracts")
CONTRACTS = (
    "canonical-observation-event.schema.json",
    "evidence-ref.schema.json",
    "extracted-fact.schema.json",
    "draft-mutation.schema.json",
    "approved-command.schema.json",
    "connector-checkpoint.schema.json",
)


def validate_contracts() -> None:
    for filename in CONTRACTS:
        payload = json.loads((CONTRACT_ROOT / filename).read_text(encoding="utf-8"))
        if payload.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError(f"{filename} is not JSON Schema 2020-12")
        if payload.get("type") != "object" or not payload.get("$id"):
            raise ValueError(f"{filename} is missing its object contract identity")


def validate_postgres_socket() -> None:
    host = os.environ.get("OBSERVER_POSTGRES_HOST", "observer-postgres")
    port = int(os.environ.get("OBSERVER_POSTGRES_PORT", "5432"))
    with socket.create_connection((host, port), timeout=5):
        pass


if __name__ == "__main__":
    validate_contracts()
    validate_postgres_socket()
    print("Observer placeholder contracts and PostgreSQL connectivity are healthy.")
