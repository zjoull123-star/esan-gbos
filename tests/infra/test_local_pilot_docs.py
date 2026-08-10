from __future__ import annotations

import hashlib
import json
import plistlib
from pathlib import Path

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs" / "local-pilot"
INFRA = ROOT / "infra" / "local"
CURRENT_SOURCE_COMMIT = "ad58ab3ea8c0d521cebd90c2642709d135f98fac"


def _read(path: Path) -> str:
    assert path.is_file(), f"required local-pilot asset is missing: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def test_runbook_documents_boundaries_and_honest_runtime_blocker() -> None:
    runbook = _read(DOCS / "RUNBOOK.md")

    for statement in (
        "127.0.0.1",
        "不删除任何命名卷",
        "保留 PostgreSQL 与 filesystem CAS",
        "真实连接器默认关闭",
        "DeepSeek 默认关闭",
        "仅 WhatsApp webhook ingress",
        "不会安装 LaunchAgent",
        "runtime entrypoint",
        "fail closed",
        "composition.status=composed",
        "Frappe PWA",
        "尚未用真实",
        "Compose config 和源绑定镜像只",
        "contracts/local_pilot",
        "Keychain",
        "0600",
        "禁止下载",
        "Kingdee",
        "WhatsApp Cloud API 不存在 poller",
        "MinIO 不属于",
        "Prometheus",
        "frappe-materializer-bootstrap",
        "migrations → materializer identity bootstrap → runtime",
        "Website User",
        "desk_access=0",
        "start-synthetic --acknowledge-synthetic",
        "不启动 connector、model、media 或 tunnel",
        "已构建并记录的本地镜像",
    ):
        assert statement in runbook
    assert "数据库、对象存储控制台" not in runbook
    assert "MinIO、Prometheus" not in runbook


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
    assert "filesystem CAS" in assertions
    assert "MinIO" in assertions
    assert "identity-resolution" in assertions
    assert "up=1" in assertions
    assert "readiness 为 `0`" in assertions
    assert "Frappe PWA 尚未组合进" not in assertions
    assert "local runtime 没有 Containerfile" not in assertions
    assert "frappe-materializer-bootstrap" in assertions
    assert "Website User" in assertions


def test_runbook_documents_closed_channel_credential_json_without_real_secrets() -> None:
    runbook = _read(DOCS / "RUNBOOK.md")

    for heading in (
        "Email credential JSON",
        "WhatsApp credential JSON",
        "WeCom credential JSON",
    ):
        assert heading in runbook
    for field in (
        '"instance_id"',
        '"team_ref"',
        '"agent_task_type"',
        '"account_user_ref"',
        '"initial_checkpoint"',
        '"app_secret"',
        '"verify_token"',
        '"private_key"',
    ):
        assert field in runbook
    email_section = runbook.split("### Email credential JSON", maxsplit=1)[1].split(
        "### WhatsApp credential JSON", maxsplit=1
    )[0]
    email_example = email_section.split("```json", maxsplit=1)[1].split("```", maxsplit=1)[0]
    email_payload = json.loads(email_example)
    assert email_payload["account_user_ref"] == "__APPROVED_FRAPPE_USER__"
    checkpoint = json.loads(email_payload["initial_checkpoint"])
    assert set(checkpoint) == {"mailbox", "uid", "uidvalidity", "version"}
    assert checkpoint["mailbox"] == email_payload["mailbox"]
    assert checkpoint["version"] == 1
    assert "整份 credential JSON" in runbook
    assert "activation_time" in runbook
    assert "blocked_official_sdk" in runbook
    assert "trusted_phrase_lexicon" in runbook
    assert "30 天" in runbook
    assert "人工 attestation" in runbook
    for field in (
        '"resolver_version"',
        '"approved_by"',
        '"approved_at"',
        '"expires_at"',
        '"names_complete"',
        '"organizations_complete"',
        '"names"',
        '"organizations"',
    ):
        assert field in runbook
    assert "sk-" not in runbook
    assert "-----BEGIN" not in runbook


def test_local_pilot_evidence_snapshot_is_redacted_and_checksumable() -> None:
    evidence_dir = ROOT / "docs" / "evidence" / "local-pilot"
    evidence_path = evidence_dir / "local-pilot-evidence.json"
    summary_path = evidence_dir / "local-pilot-summary.md"
    sums_path = evidence_dir / "SHA256SUMS"

    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["captured_at"]
    assert payload["commit_sha"].startswith("64dc48c")
    assert payload["verdict"]["formal"]["composition_status"] == "not_composed"
    assert payload["verdict"]["formal"]["local_pilot_go"] is False
    assert payload["verdict"]["formal"]["preflight_require_go"]["exit_code"] == 78
    assert payload["verdict"]["synthetic_core"]["verdict"] == "local_synthetic_observed"
    assert payload["runtime"]["networks"]["local-internal"]["internal"] is False
    assert payload["runtime"]["networks"]["webhook-tunnel"]["internal"] is True
    assert payload["browser_validation"]["console_errors"] == 0
    assert (
        payload["browser_validation"]["console_warnings"] == "not_asserted_in_current_browser_run"
    )
    assert payload["browser_validation"]["real_site_role_smoke"]["passed_role_cases"] == 7
    assert payload["browser_validation"]["real_site_role_smoke"]["storage_state_files_removed"]
    assert payload["verification_snapshot"]["status"] == "captured_snapshot_not_final_signoff"
    assert payload["recoverable_failure"]["deleted"] is False

    for text in (
        evidence_path.read_text(encoding="utf-8"),
        summary_path.read_text(encoding="utf-8"),
    ):
        assert "sk-" not in text
        assert "-----BEGIN" not in text
        assert "Cookie:" not in text

    expected = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        expected[name] = digest
    for path in (evidence_path, summary_path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert expected[path.name] == digest


def test_runbook_has_the_current_credential_free_canary_operator_sequence() -> None:
    runbook = _read(DOCS / "RUNBOOK.md")
    sequence_section = runbook.split("严格按以下顺序执行", maxsplit=1)[1].split(
        "## Keychain", maxsplit=1
    )[0]
    command_block = sequence_section.split("```sh", maxsplit=1)[1].split("```", maxsplit=1)[0]
    sequence_text = " ".join(command_block.split())

    sequence = (
        "final code",
        "governed current-image rebuild/record",
        "prepare external canary dir/control",
        "probe-email-checkpoint",
        "initial_checkpoint",
        "canary-preflight",
        "receipt",
        "start-email-deepseek-canary",
        "verify-canary-chain",
        "projection config",
        "observation window",
        "canary-evidence",
        "--chain-attestation",
        "finalize",
    )
    positions = [sequence_text.lower().find(item.lower()) for item in sequence]
    assert all(position >= 0 for position in positions), positions
    assert positions == sorted(positions)
    assert "response_reported_observed_model" in sequence_section
    assert "free-form observed model" in sequence_section.lower()
    invocation_text = "\n".join(
        line for line in command_block.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--observed-at" not in invocation_text
    assert "repo-external" in sequence_section
    assert "secrets outside" in runbook.lower()


def test_runbook_documents_current_truth_boundaries_without_unlocking_formal_state() -> None:
    runbook = _read(DOCS / "RUNBOOK.md")

    for statement in (
        CURRENT_SOURCE_COMMIT,
        "runtime code validation reference",
        "final branch includes only image-lock/test/docs successors after it",
        "response_reported_observed_model=unknown",
        "local_pilot_go=false",
        "production_go=false",
        "real Email + DeepSeek canary 未执行",
        "checked-in Email/DeepSeek disabled",
        "Kingdee",
        "cloud",
        "external send",
        "current locked runtime",
        "older source",
        "rebuild before the real canary",
        "72 小时连续运行不再作为本阶段退出条件",
        "Model fatal latch",
        "STATUS_UIDVALIDITY_UIDNEXT",
        "42 passed, 10 deselected",
        "governed dependency/image/scanner network",
        "isolated PostgreSQL validation/build/scanner containers",
    ):
        assert statement in runbook
    alert_count = _read(INFRA / "prometheus" / "alerts.yml").count("- alert:")
    assert alert_count == 7
    assert f"{alert_count} 条低基数规则健康" in runbook
