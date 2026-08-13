from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_email_gateway_postgres_runner_is_disposable_and_closed() -> None:
    script = (ROOT / "scripts" / "dev" / "test-email-gateway-postgres").read_text()

    for required in (
        "--observer-through",
        "--all",
        "pgvector/pgvector:",
        "chmod 600",
        "trap cleanup EXIT",
        "docker run",
        "-p 127.0.0.1::5432",
        "--tmpfs /var/lib/postgresql/data",
        "014_email_gateway_publication.sql",
    ):
        assert required in script
    assert "--volume" not in script
    assert "infra/dev/.env" not in script
