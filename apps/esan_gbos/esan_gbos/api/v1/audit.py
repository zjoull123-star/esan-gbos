from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import frappe

from esan_gbos.api.v1.common import BFFError, request_id
from esan_gbos.domain.idempotency import command_payload_hash


def _audit_name(key: str) -> str:
    site = getattr(frappe.local, "site", "")
    digest = hashlib.sha256(f"{site}\0{key}".encode()).hexdigest()
    return f"GBOS-IDEMP-{digest[:48]}"


def _load(name: str) -> dict[str, Any] | None:
    if not frappe.db.exists("Integration Request", name):
        return None
    doc = frappe.get_doc("Integration Request", name)
    request_data = json.loads(doc.data or "{}")
    output = json.loads(doc.output or "{}") if doc.output else None
    return {
        "payload_hash": request_data.get("payload_hash"),
        "request_id": doc.request_id,
        "status": doc.status,
        "output": output,
    }


def run_idempotent[Result: dict[str, Any]](
    command: str,
    key: str,
    payload: dict[str, Any],
    execute: Callable[[], Result],
) -> tuple[Result, bool, str]:
    name = _audit_name(key)
    digest = command_payload_hash(command, frappe.session.user, payload)
    existing = _load(name)
    if existing:
        if existing["payload_hash"] != digest:
            raise BFFError(
                "idempotency_conflict",
                "Idempotency key was already used with a different payload",
                status=409,
            )
        if existing["status"] != "Completed" or existing["output"] is None:
            raise BFFError(
                "request_in_progress",
                "An identical command is already in progress",
                status=409,
            )
        return existing["output"], True, str(existing["request_id"])

    current_request_id = request_id()
    audit = frappe.get_doc(
        {
            "doctype": "Integration Request",
            "request_id": current_request_id,
            "integration_request_service": f"esan_gbos.v1.{command}",
            "request_description": "Governed GBOS BFF command",
            "status": "Authorized",
            "data": json.dumps(
                {
                    "command": command,
                    "actor": frappe.session.user,
                    "idempotency_key": key,
                    "payload_hash": digest,
                },
                sort_keys=True,
            ),
        }
    )
    try:
        audit.insert(ignore_permissions=True, set_name=name)
    except frappe.DuplicateEntryError as error:
        existing = _load(name)
        if existing and existing["payload_hash"] == digest and existing["output"]:
            return existing["output"], True, str(existing["request_id"])
        raise BFFError(
            "request_in_progress",
            "Command is already in progress",
            status=409,
        ) from error

    result = execute()
    audit.status = "Completed"
    audit.output = json.dumps(result, sort_keys=True, default=str)
    audit.reference_doctype = result.get("doctype")
    audit.reference_docname = result.get("name")
    audit.save(ignore_permissions=True)
    return result, False, current_request_id
