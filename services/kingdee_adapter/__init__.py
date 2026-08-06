"""Gate 5 governed read-only Kingdee adapter boundary."""

from .adapter import KingdeeAdapter
from .models import (
    AdapterResponse,
    AdapterStatus,
    AuthContext,
    ControlMetrics,
    QueryPlan,
    ValidatedRequest,
    VerificationSnapshot,
    VerificationStatus,
)
from .policy import FrozenKingdeePolicy, RequestRejected
from .transport import LiveDestination, LiveEntryGates, LiveTransport, SyntheticTransport

__all__ = [
    "AdapterResponse",
    "AdapterStatus",
    "AuthContext",
    "ControlMetrics",
    "FrozenKingdeePolicy",
    "KingdeeAdapter",
    "LiveDestination",
    "LiveEntryGates",
    "LiveTransport",
    "QueryPlan",
    "RequestRejected",
    "SyntheticTransport",
    "ValidatedRequest",
    "VerificationSnapshot",
    "VerificationStatus",
]
