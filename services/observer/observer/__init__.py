"""Local, deterministic Gate 3 Observer domain core.

The package deliberately contains no provider, network, model, subprocess, or
Frappe integration.  Only inert fixture-backed ``manual_import`` is enabled.
"""

from .application import IdempotencyConflict, ManualImportPipeline, canonical_import_body
from .evidence_store import ContentAddressedEvidenceStore
from .models import (
    ByteLocator,
    ImportResult,
    ManualImportManifest,
    ManualImportMember,
    Participant,
    TenantScope,
)
from .processing import DeterministicProcessor, DisabledReviewCaseBridge
from .security import HMACServiceIdentity, LocalRequestAuthenticator, NonceStore

__all__ = [
    "ByteLocator",
    "ContentAddressedEvidenceStore",
    "DeterministicProcessor",
    "DisabledReviewCaseBridge",
    "HMACServiceIdentity",
    "IdempotencyConflict",
    "ImportResult",
    "LocalRequestAuthenticator",
    "ManualImportManifest",
    "ManualImportMember",
    "ManualImportPipeline",
    "NonceStore",
    "Participant",
    "TenantScope",
    "canonical_import_body",
]
