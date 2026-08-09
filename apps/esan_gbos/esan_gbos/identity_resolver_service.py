"""Fail-closed bench helper for the Observer identity-resolver service user."""

from __future__ import annotations

import hmac
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import frappe

from esan_gbos.domain.permissions import IDENTITY_RESOLVER_ROLE

_USER = "gbos-identity-resolver@localhost.invalid"
_ROLE = IDENTITY_RESOLVER_ROLE
_AUTH_REF = "observer-identity-resolver-v1"
_DEFAULT_SITE = "gbos.localhost"
_PURPOSES = ["identity_resolution"]
_PRODUCTION_VALUES = frozenset({"1", "true", "yes"})
_SITE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,138}[a-z0-9])?")
_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{15,128}")
_SECRET_DIRECTORY = Path("/run/secrets")
_SAFE_SECRET_FILENAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_MAX_CREDENTIAL_FILE_BYTES = 4096
_LEGACY_CREDENTIAL_ENVIRONMENT = frozenset(
    {
        "GBOS_IDENTITY_RESOLVER_API_KEY",
        "GBOS_IDENTITY_RESOLVER_API_SECRET",
    }
)
_LEGACY_CREDENTIAL_CONFIG = frozenset(
    {
        "gbos_identity_resolver_api_key",
        "gbos_identity_resolver_api_secret",
    }
)


class IdentityResolverProvisioningError(RuntimeError):
    """A non-sensitive refusal from the bench-only provisioning helper."""


def provision_identity_resolver(confirm_local_pilot: Any = False) -> dict[str, str]:
    """Create or verify the exact local-pilot resolver identity.

    This function is deliberately not whitelisted. The only permission bypass is
    the bench-operated initial User insert; an existing user is never repaired.
    """
    try:
        receipt = _provision(confirm_local_pilot)
        frappe.db.commit()
        return receipt
    except IdentityResolverProvisioningError:
        frappe.db.rollback()
        raise
    except Exception:
        frappe.db.rollback()
        raise IdentityResolverProvisioningError("identity resolver provisioning failed") from None


def _provision(confirm_local_pilot: Any) -> dict[str, str]:
    if confirm_local_pilot is not True:
        raise IdentityResolverProvisioningError("local-pilot confirmation required")
    if os.environ.get("GBOS_PRODUCTION_ENABLED", "").strip().casefold() in _PRODUCTION_VALUES:
        raise IdentityResolverProvisioningError("production environment is not allowed")
    _reject_legacy_credentials()

    site_id = _active_site()
    _validate_identity_config(site_id)
    _validate_service_role()
    api_key = _credential(
        environment_variable="GBOS_IDENTITY_RESOLVER_API_KEY_FILE",
        config_key="gbos_identity_resolver_api_key_file",
    )
    api_secret = _credential(
        environment_variable="GBOS_IDENTITY_RESOLVER_API_SECRET_FILE",
        config_key="gbos_identity_resolver_api_secret_file",
    )

    if frappe.db.exists("User", _USER):
        user = frappe.get_doc("User", _USER)
        if not _is_exact_user(user, api_key=api_key, api_secret=api_secret):
            raise IdentityResolverProvisioningError("existing identity drift")
        return _receipt("skipped", site_id)

    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": _USER,
            "first_name": "GBOS Identity Resolver Service",
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
        raise IdentityResolverProvisioningError("created identity failed verification")
    return _receipt("created", site_id)


def _active_site() -> str:
    site_id = str(getattr(frappe.local, "site", ""))
    closed_site = os.environ.get("GBOS_LOCAL_PILOT_SITE_ID")
    if _SITE.fullmatch(site_id) is None:
        raise IdentityResolverProvisioningError("active site is not allowed")
    if closed_site is not None and closed_site != site_id:
        raise IdentityResolverProvisioningError("active site is not allowed")
    if site_id != _DEFAULT_SITE and closed_site != site_id:
        raise IdentityResolverProvisioningError("active site is not allowed")
    return site_id


def _validate_identity_config(site_id: str) -> None:
    identities = frappe.conf.get("gbos_identity_resolver_identities")
    if not isinstance(identities, Mapping):
        raise IdentityResolverProvisioningError("identity configuration is invalid")
    identity = identities.get(_AUTH_REF)
    if not isinstance(identity, Mapping) or set(identity) != {
        "user",
        "site_id",
        "processing_purposes",
    }:
        raise IdentityResolverProvisioningError("identity configuration is invalid")
    if (
        identity.get("user") != _USER
        or identity.get("site_id") != site_id
        or identity.get("processing_purposes") != _PURPOSES
    ):
        raise IdentityResolverProvisioningError("identity configuration is invalid")


def _validate_service_role() -> None:
    if not frappe.db.exists("Role", _ROLE):
        raise IdentityResolverProvisioningError("service role is invalid")
    if frappe.db.get_value("Role", _ROLE, "desk_access") != 0:
        raise IdentityResolverProvisioningError("service role is invalid")


def _reject_legacy_credentials() -> None:
    if any(variable in os.environ for variable in _LEGACY_CREDENTIAL_ENVIRONMENT) or any(
        frappe.conf.get(key) is not None for key in _LEGACY_CREDENTIAL_CONFIG
    ):
        raise IdentityResolverProvisioningError("legacy credential environment is not allowed")


def _credential(*, environment_variable: str, config_key: str) -> str:
    environment_path = os.environ.get(environment_variable)
    config_path = frappe.conf.get(config_key)
    if environment_path is not None and config_path is not None:
        raise IdentityResolverProvisioningError("credential file is invalid")
    selected = environment_path if environment_path is not None else config_path
    if not isinstance(selected, str):
        raise IdentityResolverProvisioningError("credential file is invalid")
    return _read_credential_file(selected)


def _read_credential_file(value: str) -> str:
    path = Path(value)
    if (
        not path.is_absolute()
        or path.parent != _SECRET_DIRECTORY
        or _SAFE_SECRET_FILENAME.fullmatch(path.name) is None
    ):
        raise IdentityResolverProvisioningError("credential file is invalid")

    try:
        before = path.lstat()
        if not _safe_credential_file(before):
            raise IdentityResolverProvisioningError("credential file is invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not _same_safe_credential_file(before, opened):
                raise IdentityResolverProvisioningError("credential file is invalid")
            raw = bytearray()
            while len(raw) <= _MAX_CREDENTIAL_FILE_BYTES:
                chunk = os.read(
                    descriptor,
                    _MAX_CREDENTIAL_FILE_BYTES + 1 - len(raw),
                )
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(descriptor)
            if not _same_safe_credential_file(opened, after) or len(raw) != after.st_size:
                raise IdentityResolverProvisioningError("credential file is invalid")
        finally:
            os.close(descriptor)
    except IdentityResolverProvisioningError:
        raise
    except OSError:
        raise IdentityResolverProvisioningError("credential file is invalid") from None

    if not 0 < len(raw) <= _MAX_CREDENTIAL_FILE_BYTES:
        raise IdentityResolverProvisioningError("credential file is invalid")
    try:
        credential = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise IdentityResolverProvisioningError("credential file is invalid") from None
    if credential.endswith("\n"):
        credential = credential[:-1]
    if _CREDENTIAL.fullmatch(credential) is None:
        raise IdentityResolverProvisioningError("credential file is invalid")
    return credential


def _safe_credential_file(details: os.stat_result) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and stat.S_IMODE(details.st_mode) in {0o400, 0o600}
        and 0 < details.st_size <= _MAX_CREDENTIAL_FILE_BYTES
    )


def _same_safe_credential_file(
    expected: os.stat_result,
    observed: os.stat_result,
) -> bool:
    return _safe_credential_file(observed) and (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
    ) == (observed.st_dev, observed.st_ino, observed.st_size)


def _is_exact_user(user: Any, *, api_key: str, api_secret: str) -> bool:
    roles = {_role_name(row) for row in (user.get("roles") or [])}
    stored_key = user.get("api_key")
    stored_secret = user.get_password("api_secret", raise_exception=False)
    return (
        user.get("email") == _USER
        and user.get("enabled") == 1
        and user.get("send_welcome_email") in (None, 0)
        and user.get("user_type") == "Website User"
        and roles == {_ROLE}
        and user.get("role_profile_name") in (None, "")
        and not (user.get("role_profiles") or [])
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


__all__ = [
    "IdentityResolverProvisioningError",
    "provision_identity_resolver",
]
