from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol, cast

from .decision_storage import (
    DecisionStorage,
    EvidenceSnapshot,
    FactSnapshot,
    ProposalSnapshot,
)


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


def _document(value: object) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("database document must be a JSON object")
    return cast(dict[str, Any], parsed)


def _timestamp(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field} must be a timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


class PostgresDecisionStorage(DecisionStorage):
    """Transactional, RLS-scoped persistence for Gate 4 decisions and facts."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get_proposal(self, site_id: str, proposal_ref: str) -> ProposalSnapshot | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT processing_purpose, proposal_version, proposal_revision,
                       subject_ref, predicate, document, payload_digest, recorded_at
                FROM context.fact_proposals
                WHERE site_id = %s AND fact_proposal_record_id = %s
                """,
                (site_id, proposal_ref),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                SELECT evidence_record_id
                FROM context.fact_evidence
                WHERE site_id = %s AND fact_proposal_record_id = %s
                ORDER BY evidence_record_id
                """,
                (site_id, proposal_ref),
            )
            evidence_refs = tuple(str(value[0]) for value in cursor.fetchall())
        document = _document(row[5])
        fact = _document(document.get("fact"))
        valid_time = _document(document.get("valid_time"))
        source_lineage = _document(document.get("source_lineage"))
        proposal_version = row[1]
        if not isinstance(proposal_version, str) or not proposal_version:
            raise ValueError("proposal_version is missing from the governed proposal")
        return ProposalSnapshot(
            site_id=site_id,
            processing_purpose=str(row[0]),
            proposal_ref=proposal_ref,
            proposal_version=proposal_version,
            proposal_revision=int(row[2]),
            subject_ref=str(row[3]),
            predicate=str(row[4]),
            value=_document(fact.get("value")),
            evidence_refs=evidence_refs,
            valid_start=_timestamp(valid_time.get("start"), "valid_time.start"),
            valid_end=(
                None
                if valid_time.get("end") is None
                else _timestamp(valid_time["end"], "valid_time.end")
            ),
            recorded_time=_timestamp(
                document.get("recorded_time", row[7]),
                "recorded_time",
            ),
            source_lineage=source_lineage,
            payload_digest=str(row[6]),
        )

    def get_evidence(self, site_id: str, evidence_ref: str) -> EvidenceSnapshot | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT document
                FROM context.evidence_records
                WHERE site_id = %s AND evidence_record_id = %s
                """,
                (site_id, evidence_ref),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return EvidenceSnapshot(
            site_id=site_id,
            evidence_record_id=evidence_ref,
            document=_document(row[0]),
        )

    def get_fact(
        self,
        site_id: str,
        fact_id: str,
        fact_version: int,
    ) -> FactSnapshot | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT document
                FROM context.verified_facts
                WHERE site_id = %s AND fact_id = %s AND fact_version = %s
                """,
                (site_id, fact_id, fact_version),
            )
            row = cursor.fetchone()
        return None if row is None else FactSnapshot.from_document(_document(row[0]))

    def get_current_fact(
        self,
        site_id: str,
        subject_ref: str,
        predicate: str,
    ) -> FactSnapshot | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT current.document
                FROM context.verified_facts AS current
                WHERE current.site_id = %s
                  AND current.subject_ref = %s
                  AND current.predicate = %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM context.verified_facts AS successor
                      WHERE successor.site_id = current.site_id
                        AND successor.supersedes_fact_id = current.fact_id
                        AND successor.supersedes_fact_version = current.fact_version
                  )
                ORDER BY current.fact_version DESC
                LIMIT 1
                """,
                (site_id, subject_ref, predicate),
            )
            row = cursor.fetchone()
        return None if row is None else FactSnapshot.from_document(_document(row[0]))

    def save_conflict(
        self,
        *,
        conflict: dict[str, Any],
        expected_proposal_version: str,
        expected_proposal_revision: int,
    ) -> None:
        from .decision import StaleRevision

        site_id = str(conflict["site_id"])
        proposal_ref = str(conflict["candidates"][1]["record_ref"])
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            self._lock_proposal(
                cursor,
                site_id=site_id,
                proposal_ref=proposal_ref,
                expected_version=expected_proposal_version,
                expected_revision=expected_proposal_revision,
                error_type=StaleRevision,
            )
            cursor.execute(
                """
                INSERT INTO context.conflicts (
                    site_id, conflict_id, processing_purpose, proposal_ref,
                    status, detected_at, recorded_time, document
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    site_id,
                    conflict["conflict_id"],
                    conflict["processing_purpose"],
                    proposal_ref,
                    conflict["status"],
                    conflict["detected_at"],
                    conflict["recorded_time"],
                    json.dumps(conflict, ensure_ascii=False, sort_keys=True),
                ),
            )
            for candidate in conflict["candidates"]:
                if candidate["record_kind"] == "verified_fact":
                    cursor.execute(
                        """
                        INSERT INTO context.conflict_fact_refs (
                            site_id, conflict_id, fact_id, fact_version
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (
                            site_id,
                            conflict["conflict_id"],
                            candidate["record_ref"],
                            candidate["record_revision"],
                        ),
                    )
            for evidence_ref in conflict["evidence_refs"]:
                cursor.execute(
                    """
                    INSERT INTO context.conflict_evidence_refs (
                        site_id, conflict_id, evidence_record_id
                    ) VALUES (%s, %s, %s)
                    """,
                    (site_id, conflict["conflict_id"], evidence_ref),
                )

    def save_confirmation(
        self,
        *,
        decision: dict[str, Any],
        fact: dict[str, Any],
        expected_proposal_version: str,
        expected_proposal_revision: int,
        expected_current_fact_ref: str | None,
        expected_current_fact_version: int | None,
    ) -> None:
        from .decision import StaleRevision

        site_id = str(decision["site_id"])
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            self._lock_proposal(
                cursor,
                site_id=site_id,
                proposal_ref=str(decision["proposal_ref"]),
                expected_version=expected_proposal_version,
                expected_revision=expected_proposal_revision,
                error_type=StaleRevision,
            )
            current_identity = self._lock_current_fact(
                cursor,
                site_id=site_id,
                subject_ref=str(fact["subject_ref"]),
                predicate=str(fact["predicate"]),
            )
            if current_identity != (
                expected_current_fact_ref,
                expected_current_fact_version,
            ):
                raise StaleRevision("current fact changed during confirmation")

            cursor.execute(
                """
                INSERT INTO context.decisions (
                    site_id, decision_id, decision_revision, processing_purpose,
                    proposal_ref, proposal_version, proposal_revision,
                    decision_type, operator_ref, rule_version, valid_start,
                    valid_end, effective_at, recorded_time, document
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s::jsonb
                )
                """,
                (
                    site_id,
                    decision["decision_id"],
                    decision["decision_revision"],
                    decision["processing_purpose"],
                    decision["proposal_ref"],
                    decision["proposal_version"],
                    decision["proposal_revision"],
                    decision["decision_type"],
                    decision["operator"],
                    decision.get("rule_version"),
                    decision["valid_time"]["start"],
                    decision["valid_time"]["end"],
                    decision["effective_at"],
                    decision["recorded_time"],
                    json.dumps(decision, ensure_ascii=False, sort_keys=True),
                ),
            )
            cursor.execute(
                """
                INSERT INTO context.verified_facts (
                    site_id, fact_id, fact_version, processing_purpose,
                    proposal_ref, proposal_version, proposal_revision,
                    subject_ref, predicate, valid_start, valid_end, recorded_time,
                    confirmation_decision_id, confirmation_decision_revision,
                    supersedes_fact_id, supersedes_fact_version, document
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                )
                """,
                (
                    site_id,
                    fact["fact_id"],
                    fact["fact_version"],
                    fact["processing_purpose"],
                    fact["proposal_ref"],
                    fact["proposal_version"],
                    fact["proposal_revision"],
                    fact["subject_ref"],
                    fact["predicate"],
                    fact["valid_time"]["start"],
                    fact["valid_time"]["end"],
                    fact["recorded_time"],
                    fact["confirmation_decision_ref"],
                    decision["decision_revision"],
                    fact.get("supersedes_fact_ref"),
                    fact.get("supersedes_fact_version"),
                    json.dumps(fact, ensure_ascii=False, sort_keys=True),
                ),
            )
            for role, references in (
                ("input", decision["input_fact_refs"]),
                ("output", decision["output_fact_refs"]),
            ):
                for reference in references:
                    cursor.execute(
                        """
                        INSERT INTO context.decision_fact_refs (
                            site_id, decision_id, decision_revision,
                            ref_role, fact_id, fact_version
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            site_id,
                            decision["decision_id"],
                            decision["decision_revision"],
                            role,
                            reference["fact_id"],
                            reference["fact_version"],
                        ),
                    )
            for evidence_ref in decision["evidence_refs"]:
                cursor.execute(
                    """
                    INSERT INTO context.decision_evidence_refs (
                        site_id, decision_id, decision_revision, evidence_record_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (
                        site_id,
                        decision["decision_id"],
                        decision["decision_revision"],
                        evidence_ref,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO context.fact_evidence_refs (
                        site_id, fact_id, fact_version, evidence_record_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (
                        site_id,
                        fact["fact_id"],
                        fact["fact_version"],
                        evidence_ref,
                    ),
                )

    def get_decision(self, site_id: str, decision_id: str) -> dict[str, Any] | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT document
                FROM context.decisions
                WHERE site_id = %s AND decision_id = %s
                ORDER BY decision_revision DESC
                LIMIT 1
                """,
                (site_id, decision_id),
            )
            row = cursor.fetchone()
        return None if row is None else _document(row[0])

    def get_decision_fact_refs(
        self,
        site_id: str,
        decision_id: str,
        decision_revision: int,
    ) -> tuple[tuple[str, str, int], ...]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT ref_role, fact_id, fact_version
                FROM context.decision_fact_refs
                WHERE site_id = %s
                  AND decision_id = %s
                  AND decision_revision = %s
                ORDER BY ref_role, fact_id, fact_version
                """,
                (site_id, decision_id, decision_revision),
            )
            return tuple((str(row[0]), str(row[1]), int(row[2])) for row in cursor.fetchall())

    def get_decision_evidence_refs(
        self,
        site_id: str,
        decision_id: str,
        decision_revision: int,
    ) -> tuple[str, ...]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT evidence_record_id
                FROM context.decision_evidence_refs
                WHERE site_id = %s
                  AND decision_id = %s
                  AND decision_revision = %s
                ORDER BY evidence_record_id
                """,
                (site_id, decision_id, decision_revision),
            )
            return tuple(str(row[0]) for row in cursor.fetchall())

    def get_fact_evidence_refs(
        self,
        site_id: str,
        fact_id: str,
        fact_version: int,
    ) -> tuple[str, ...]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT evidence_record_id
                FROM context.fact_evidence_refs
                WHERE site_id = %s
                  AND fact_id = %s
                  AND fact_version = %s
                ORDER BY evidence_record_id
                """,
                (site_id, fact_id, fact_version),
            )
            return tuple(str(row[0]) for row in cursor.fetchall())

    @staticmethod
    def _set_site(cursor: Cursor, site_id: str) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))

    @staticmethod
    def _lock_proposal(
        cursor: Cursor,
        *,
        site_id: str,
        proposal_ref: str,
        expected_version: str,
        expected_revision: int,
        error_type: type[ValueError],
    ) -> None:
        cursor.execute(
            """
            SELECT proposal_version, proposal_revision
            FROM context.fact_proposals
            WHERE site_id = %s AND fact_proposal_record_id = %s
            """,
            (site_id, proposal_ref),
        )
        row = cursor.fetchone()
        if row is None or str(row[0]) != expected_version or int(row[1]) != expected_revision:
            raise error_type("proposal changed during decision persistence")

    @staticmethod
    def _lock_current_fact(
        cursor: Cursor,
        *,
        site_id: str,
        subject_ref: str,
        predicate: str,
    ) -> tuple[str | None, int | None]:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{site_id}\x1f{subject_ref}\x1f{predicate}",),
        )
        cursor.execute(
            """
            SELECT current.fact_id, current.fact_version
            FROM context.verified_facts AS current
            WHERE current.site_id = %s
              AND current.subject_ref = %s
              AND current.predicate = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM context.verified_facts AS successor
                  WHERE successor.site_id = current.site_id
                    AND successor.supersedes_fact_id = current.fact_id
                    AND successor.supersedes_fact_version = current.fact_version
            )
            ORDER BY current.fact_version DESC
            LIMIT 1
            """,
            (site_id, subject_ref, predicate),
        )
        row = cursor.fetchone()
        return (None, None) if row is None else (str(row[0]), int(row[1]))
