"""Fail-closed bench helper for the Frappe command-publication consumer."""

from __future__ import annotations

import hmac
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import frappe

from esan_gbos.domain.permissions import EMAIL_COMMAND_PUBLICATION_ROLE

_USER = "email-command-publication@localhost.invalid"
_ROLE = EMAIL_COMMAND_PUBLICATION_ROLE
_AUTH_REF = "email-command-publication-v1"
_DEFAULT_SITE = "gbos.localhost"
_PURPOSES = ["email_command_publication"]
_PRODUCTION_VALUES = frozenset({"1", "true", "yes"})
_SITE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,138}[a-z0-9])?")
_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{15,128}")
_SECRET_DIRECTORY = Path("/run/secrets")
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_MAX_BYTES = 4096


class EmailCommandPublicationProvisioningError(RuntimeError):
    """A non-sensitive provisioning refusal."""


def provision_email_command_publication(confirm_local_pilot: Any = False) -> dict[str, str]:
    try:
        receipt = _provision(confirm_local_pilot)
        frappe.db.commit()
        return receipt
    except EmailCommandPublicationProvisioningError:
        frappe.db.rollback()
        raise
    except Exception:
        frappe.db.rollback()
        raise EmailCommandPublicationProvisioningError(
            "command publication provisioning failed"
        ) from None


def _provision(confirm_local_pilot: Any) -> dict[str, str]:
    if confirm_local_pilot is not True:
        raise EmailCommandPublicationProvisioningError("local-pilot confirmation required")
    if os.environ.get("GBOS_PRODUCTION_ENABLED", "").strip().casefold() in _PRODUCTION_VALUES:
        raise EmailCommandPublicationProvisioningError("production environment is not allowed")
    if any(
        key in os.environ
        for key in (
            "GBOS_EMAIL_COMMAND_PUBLICATION_API_KEY",
            "GBOS_EMAIL_COMMAND_PUBLICATION_API_SECRET",
        )
    ) or any(
        frappe.conf.get(key) is not None
        for key in (
            "gbos_email_command_publication_api_key",
            "gbos_email_command_publication_api_secret",
        )
    ):
        raise EmailCommandPublicationProvisioningError(
            "legacy credential environment is not allowed"
        )
    site_id = _active_site()
    _validate_config(site_id)
    _validate_role()
    api_key = _credential(
        "GBOS_EMAIL_COMMAND_PUBLICATION_API_KEY_FILE",
        "gbos_email_command_publication_api_key_file",
    )
    api_secret = _credential(
        "GBOS_EMAIL_COMMAND_PUBLICATION_API_SECRET_FILE",
        "gbos_email_command_publication_api_secret_file",
    )
    if frappe.db.exists("User", _USER):
        user = frappe.get_doc("User", _USER)
        if not _is_exact_user(user, api_key, api_secret):
            raise EmailCommandPublicationProvisioningError("existing identity drift")
        return _receipt("skipped", site_id)
    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": _USER,
            "first_name": "Email Command Publication Service",
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
    if not _is_exact_user(persisted, api_key, api_secret):
        raise EmailCommandPublicationProvisioningError("created identity failed verification")
    return _receipt("created", site_id)


def _active_site() -> str:
    site = str(getattr(frappe.local, "site", ""))
    closed = os.environ.get("GBOS_LOCAL_PILOT_SITE_ID")
    if _SITE.fullmatch(site) is None or (closed is not None and closed != site):
        raise EmailCommandPublicationProvisioningError("active site is not allowed")
    if site != _DEFAULT_SITE and closed != site:
        raise EmailCommandPublicationProvisioningError("active site is not allowed")
    return site


def _validate_config(site: str) -> None:
    identities = frappe.conf.get("gbos_email_command_publication_identities")
    identity = identities.get(_AUTH_REF) if isinstance(identities, Mapping) else None
    if (
        not isinstance(identity, Mapping)
        or set(identity) != {"user", "site_id", "processing_purposes"}
        or identity.get("user") != _USER
        or identity.get("site_id") != site
        or identity.get("processing_purposes") != _PURPOSES
    ):
        raise EmailCommandPublicationProvisioningError("identity configuration is invalid")


def _validate_role() -> None:
    if (
        not frappe.db.exists("Role", _ROLE)
        or frappe.db.get_value("Role", _ROLE, "desk_access") != 0
    ):
        raise EmailCommandPublicationProvisioningError("service role is invalid")


def _credential(environment_key: str, config_key: str) -> str:
    environment_path = os.environ.get(environment_key)
    config_path = frappe.conf.get(config_key)
    if environment_path is not None and config_path is not None:
        raise EmailCommandPublicationProvisioningError("credential file is invalid")
    selected = environment_path if environment_path is not None else config_path
    if not isinstance(selected, str):
        raise EmailCommandPublicationProvisioningError("credential file is invalid")
    path = Path(selected)
    if (
        not path.is_absolute()
        or path.parent != _SECRET_DIRECTORY
        or _SAFE_FILENAME.fullmatch(path.name) is None
    ):
        raise EmailCommandPublicationProvisioningError("credential file is invalid")
    try:
        before = path.lstat()
        if not _safe_file(before):
            raise EmailCommandPublicationProvisioningError("credential file is invalid")
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            opened = os.fstat(descriptor)
            if not _same_file(before, opened):
                raise EmailCommandPublicationProvisioningError("credential file is invalid")
            raw = os.read(descriptor, _MAX_BYTES + 1)
            after = os.fstat(descriptor)
            if not _same_file(opened, after) or len(raw) != after.st_size:
                raise EmailCommandPublicationProvisioningError("credential file is invalid")
        finally:
            os.close(descriptor)
    except EmailCommandPublicationProvisioningError:
        raise
    except OSError:
        raise EmailCommandPublicationProvisioningError("credential file is invalid") from None
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise EmailCommandPublicationProvisioningError("credential file is invalid") from None
    if value.endswith("\n"):
        value = value[:-1]
    if _CREDENTIAL.fullmatch(value) is None:
        raise EmailCommandPublicationProvisioningError("credential file is invalid")
    return value


def _safe_file(details: os.stat_result) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and stat.S_IMODE(details.st_mode) in {0o400, 0o600}
        and 0 < details.st_size <= _MAX_BYTES
    )


def _same_file(expected: os.stat_result, actual: os.stat_result) -> bool:
    return _safe_file(actual) and (expected.st_dev, expected.st_ino, expected.st_size) == (
        actual.st_dev,
        actual.st_ino,
        actual.st_size,
    )


def _role_name(row: Any) -> str:
    return str(row.get("role") if isinstance(row, Mapping) else getattr(row, "role", ""))


def _is_exact_user(user: Any, api_key: str, api_secret: str) -> bool:
    stored_key = user.get("api_key")
    stored_secret = user.get_password("api_secret", raise_exception=False)
    return bool(
        user.get("email") == _USER
        and user.get("enabled") == 1
        and user.get("send_welcome_email") in {None, 0}
        and user.get("user_type") == "Website User"
        and {_role_name(row) for row in (user.get("roles") or [])} == {_ROLE}
        and user.get("role_profile_name") in {None, ""}
        and not (user.get("role_profiles") or [])
        and isinstance(stored_key, str)
        and isinstance(stored_secret, str)
        and hmac.compare_digest(stored_key, api_key)
        and hmac.compare_digest(stored_secret, api_secret)
    )


def _receipt(status: str, site: str) -> dict[str, str]:
    return {
        "status": status,
        "user": _USER,
        "role": _ROLE,
        "auth_ref": _AUTH_REF,
        "site_id": site,
    }


__all__ = [
    "EmailCommandPublicationProvisioningError",
    "provision_email_command_publication",
]
