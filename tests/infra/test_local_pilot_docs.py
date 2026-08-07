from __future__ import annotations

import plistlib
from pathlib import Path

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs" / "local-pilot"
INFRA = ROOT / "infra" / "local"


def _read(path: Path) -> str:
    assert path.is_file(), f"required local-pilot asset is missing: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def test_runbook_documents_boundaries_and_honest_runtime_blocker() -> None:
    runbook = _read(DOCS / "RUNBOOK.md")

    for statement in (
        "127.0.0.1",
        "不删除任何命名卷",
        "保留 PostgreSQL 与对象存储",
        "真实连接器默认关闭",
        "DeepSeek 默认关闭",
        "仅 WhatsApp webhook ingress",
        "不会安装 LaunchAgent",
        "runtime entrypoint",
        "fail closed",
        "contracts/local_pilot",
        "Keychain",
        "0600",
        "禁止下载",
        "Kingdee",
    ):
        assert statement in runbook


def test_launchagent_is_an_inert_template_not_an_installed_autostart() -> None:
    template = INFRA / "launchagents" / "com.esan.gbos.local-pilot.plist.template"
    raw = template.read_bytes()
    payload = plistlib.loads(raw)

    assert payload["RunAtLoad"] is False
    assert payload["KeepAlive"] is False
    assert "__REPO_ROOT__" in " ".join(payload["ProgramArguments"])
    assert "launchctl" not in raw.decode("utf-8")
    assert not (Path.home() / "Library" / "LaunchAgents" / template.name).exists()


def test_no_cloud_no_kingdee_no_outbound_assertions_are_operator_visible() -> None:
    assertions = _read(DOCS / "SAFETY_ASSERTIONS.md")

    assert "NO-CLOUD" in assertions
    assert "NO-KINGDEE" in assertions
    assert "NO-OUTBOUND-BY-DEFAULT" in assertions
    assert "external_send=false" in assertions
    assert "cloud_server=false" in assertions
    assert "cloud_business_storage=false" in assertions
    assert "kingdee=false" in assertions
    assert "controlled-egress" in assertions
