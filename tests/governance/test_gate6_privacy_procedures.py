from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
PROCEDURE = ROOT / "docs" / "governance" / "gate6" / "privacy-operations.md"


def test_procedure_defines_separated_roles_and_all_workflows() -> None:
    text = PROCEDURE.read_text(encoding="utf-8")

    for phrase in (
        "Requester",
        "Privacy reviewer",
        "Data owner",
        "Executor",
        "Auditor",
        "one person",
        "access/export",
        "consent withdrawal",
        "legal hold",
        "cross-border entry gate",
        "audit export",
    ):
        assert phrase.lower() in text.lower()


def test_procedure_forbids_sensitive_log_and_evidence_content() -> None:
    text = PROCEDURE.read_text(encoding="utf-8").lower()

    for phrase in (
        "secrets",
        "tokens",
        "raw communication bodies",
        "full phone numbers",
        "full email addresses",
    ):
        assert phrase in text


def test_procedure_preserves_external_input_status_and_no_legal_conclusion() -> None:
    text = PROCEDURE.read_text(encoding="utf-8")

    assert "`blocked_external_input`" in text
    assert "formal Privacy/Legal" in text
    assert "real personal data" in text
    assert "Singapore cross-border approval" in text
    assert "not legal advice" in text.lower()
