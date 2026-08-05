from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
DEMO_LOADER = ROOT / "apps" / "esan_gbos" / "esan_gbos" / "demo.py"


def test_demo_loader_is_explicit_fail_closed_and_idempotent() -> None:
    source = DEMO_LOADER.read_text(encoding="utf-8")

    for required in (
        "confirm_synthetic",
        "GBOS_PRODUCTION_ENABLED",
        "frappe_payload_sha256",
        "gbos_fixture_seed",
        "set_name=",
        "frappe.db.exists",
        "frappe.db.commit",
    ):
        assert required in source
    assert "delete_doc" not in source
    assert "frappe.db.truncate" not in source


def test_final_image_contains_the_committed_fixture_payload() -> None:
    builder = (ROOT / "scripts" / "dev" / "build-custom-image").read_text(encoding="utf-8")
    containerfile = (ROOT / "infra" / "dev" / "Containerfile.final").read_text(encoding="utf-8")

    assert "git archive" in builder
    assert "fixtures/gate1" in builder
    assert "COPY --chown=frappe:frappe fixtures/gate1" in containerfile


def test_local_bootstrap_enables_only_explicit_synthetic_fixture_seeding() -> None:
    compose = (ROOT / "infra" / "dev" / "compose.yml").read_text(encoding="utf-8")
    example = (ROOT / "infra" / "dev" / ".env.example").read_text(encoding="utf-8")

    assert "esan_gbos.demo.seed" in compose
    assert "GBOS_LOAD_DEMO_FIXTURES" in compose
    assert "GBOS_LOAD_DEMO_FIXTURES=true" in example
    assert "GBOS_DEMO_PASSWORD=SYNTHETIC-" in example
