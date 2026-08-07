"""Fail-closed bench helper for the local-pilot materializer identity."""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping
from typing import Any

import frappe

_USER = "gbos-materializer@localhost.invalid"
_ROLE = "Agent TrustedMaterializer"
_AUTH_REF = "agent-materializer-v1"
_DEFAULT_SITE = "gbos.localhost"
_PURPOSES = frozenset({"observation_processing", "sales_follow_up"})
_PRODUCTION_VALUES = frozenset({"1", "true", "yes"})
_SITE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,138}[a-z0-9])?")
_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{15,128}")


class LocalPilotProvisioningError(RuntimeError):
    """A non-sensitive local-pilot provisioning refusal."""


def provision_materializer(confirm_local_pilot: Any = False) -> dict[str, str]:
    """Provision the closed local-pilot service identity from a bench command.

    This function is intentionally not whitelisted. The sole permission bypass is
    the initial User insert because the caller is a bench operator, not a Desk
    session. Existing users are never rewritten or silently repaired.
    """
    try:
        receipt = _provision(confirm_local_pilot)
        frappe.db.commit()
        return receipt
    except LocalPilotProvisioningError:
        frappe.db.rollback()
        raise
    except Exception:
        frappe.db.rollback()
        raise LocalPilotProvisioningError("materializer provisioning failed") from None


def _provision(confirm_local_pilot: Any) -> dict[str, str]:
    if confirm_local_pilot is not True:
        raise LocalPilotProvisioningError("local-pilot confirmation required")
    if os.environ.get("GBOS_PRODUCTION_ENABLED", "").strip().casefold() in _PRODUCTION_VALUES:
        raise LocalPilotProvisioningError("production environment is not allowed")

    site_id = _active_site()
    _validate_identity_config(site_id)
    _validate_service_role()
    api_key = _credential("GBOS_MATERIALIZER_API_KEY")
    api_secret = _credential("GBOS_MATERIALIZER_API_SECRET")

    if frappe.db.exists("User", _USER):
        user = frappe.get_doc("User", _USER)
        if not _is_exact_user(user, api_key=api_key, api_secret=api_secret):
            raise LocalPilotProvisioningError("existing identity drift")
        return _receipt("skipped", site_id)

    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": _USER,
            "first_name": "GBOS Materializer Service",
            "enabled": 1,
            "send_welcome_email": 0,
            "api_key": api_key,
            "api_secret": api_secret,
            "roles": [{"role": _ROLE}],
            "role_profile_name": None,
            "role_profiles": [],
        }
    )
    user.flags.no_welcome_mail = True
    user.insert(ignore_permissions=True)

    persisted = frappe.get_doc("User", _USER)
    if not _is_exact_user(persisted, api_key=api_key, api_secret=api_secret):
        raise LocalPilotProvisioningError("created identity failed verification")
    return _receipt("created", site_id)


def _active_site() -> str:
    site_id = str(getattr(frappe.local, "site", ""))
    closed_site = os.environ.get("GBOS_LOCAL_PILOT_SITE_ID")
    if _SITE.fullmatch(site_id) is None:
        raise LocalPilotProvisioningError("active site is not allowed")
    if closed_site is not None and closed_site != site_id:
        raise LocalPilotProvisioningError("active site is not allowed")
    if site_id != _DEFAULT_SITE and closed_site != site_id:
        raise LocalPilotProvisioningError("active site is not allowed")
    return site_id


def _validate_identity_config(site_id: str) -> None:
    identities = frappe.conf.get("gbos_agent_materialization_identities")
    if not isinstance(identities, Mapping):
        raise LocalPilotProvisioningError("identity configuration is invalid")
    identity = identities.get(_AUTH_REF)
    if not isinstance(identity, Mapping) or set(identity) != {
        "user",
        "site_id",
        "processing_purposes",
    }:
        raise LocalPilotProvisioningError("identity configuration is invalid")
    purposes = identity.get("processing_purposes")
    if (
        identity.get("user") != _USER
        or identity.get("site_id") != site_id
        or not isinstance(purposes, list)
        or len(purposes) != len(_PURPOSES)
        or not all(isinstance(item, str) for item in purposes)
        or frozenset(purposes) != _PURPOSES
    ):
        raise LocalPilotProvisioningError("identity configuration is invalid")


def _validate_service_role() -> None:
    if not frappe.db.exists("Role", _ROLE):
        raise LocalPilotProvisioningError("service role is invalid")
    if frappe.db.get_value("Role", _ROLE, "desk_access") != 0:
        raise LocalPilotProvisioningError("service role is invalid")


def _credential(variable: str) -> str:
    value = os.environ.get(variable)
    if value is None or _CREDENTIAL.fullmatch(value) is None:
        raise LocalPilotProvisioningError("materializer credential is invalid")
    return value


def _is_exact_user(user: Any, *, api_key: str, api_secret: str) -> bool:
    roles = {_role_name(row) for row in (user.get("roles") or [])}
    role_profiles = user.get("role_profiles") or []
    stored_key = user.get("api_key")
    stored_secret = user.get_password("api_secret", raise_exception=False)
    return (
        user.get("email") == _USER
        and user.get("enabled") == 1
        and user.get("send_welcome_email") in (None, 0)
        and user.get("user_type") == "Website User"
        and roles == {_ROLE}
        and user.get("role_profile_name") in (None, "")
        and not role_profiles
        and isinstance(stored_key, str)
        and isinstance(stored_secret, str)
        and hmac.compare_digest(stored_key, api_key)
        and hmac.compare_digest(stored_secret, api_secret)
    )


def _role_name(row: Any) -> str:
    if isinstance(row, Mapping):
        return str(row.get("role") or "")
    return str(getattr(row, "role", ""))


def _receipt(status: str, site_id: str) -> dict[str, str]:
    return {
        "status": status,
        "user": _USER,
        "role": _ROLE,
        "auth_ref": _AUTH_REF,
        "site_id": site_id,
    }
