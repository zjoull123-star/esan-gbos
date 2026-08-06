from __future__ import annotations

import json
import re

from .conftest import ROOT

PINNED_IMAGE = re.compile(r"^[^\s@:]+(?:/[^\s@:]+)+@sha256:[0-9a-f]{64}$")
REQUIRED_COMPONENTS = {
    "app",
    "mariadb",
    "postgres_pgvector",
    "queue_cache",
    "object_storage",
    "ingress_waf",
    "monitoring",
}


def load(name: str) -> dict[str, object]:
    return json.loads((ROOT / "infra/prod" / name).read_text(encoding="utf-8"))


def test_single_tenant_topology_is_versioned_pinned_and_disabled() -> None:
    topology = load("single-tenant-v1.json")

    assert topology["version"] == 1
    assert topology["enabled"] is False
    assert topology["environment"]["tenant_mode"] == "single"
    assert set(topology["components"]) == REQUIRED_COMPONENTS
    assert all(
        PINNED_IMAGE.fullmatch(component["image"]) for component in topology["components"].values()
    )
    assert all(value is False for value in topology["capabilities"].values())


def test_topology_separates_identities_and_keeps_data_ports_private() -> None:
    topology = load("single-tenant-v1.json")
    components = topology["components"]
    identities = [component["identity"] for component in components.values()]

    assert len(identities) == len(set(identities))
    for name in ("mariadb", "postgres_pgvector", "queue_cache", "object_storage"):
        assert components[name]["public_ports"] == []
    assert components["ingress_waf"]["public_ports"] == [443]
    assert topology["backup"]["identity"] not in identities
    assert topology["secrets"]["kms_key_ref"] != topology["backup"]["kms_key_ref"]


def test_future_site_per_tenant_template_is_inert_and_isolated() -> None:
    topology = load("site-per-tenant-v1.template.json")

    assert topology["enabled"] is False
    assert topology["template"]["render_required"] is True
    assert topology["environment"]["tenant_mode"] == "site-per-tenant"
    assert "${TENANT_ID}" in topology["environment"]["tenant_id"]
    assert all(value is False for value in topology["capabilities"].values())
    assert all(
        "${TENANT_ID}" in component["identity"] for component in topology["components"].values()
    )
