from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from services.kingdee_adapter import AuthContext, FrozenKingdeePolicy, KingdeeAdapter
from services.kingdee_adapter.transport import SyntheticTransport


@pytest.fixture
def policy() -> FrozenKingdeePolicy:
    return FrozenKingdeePolicy.load()


@pytest.fixture
def adapter(policy: FrozenKingdeePolicy) -> KingdeeAdapter:
    return KingdeeAdapter(policy=policy, transport=SyntheticTransport())


@pytest.fixture
def auth() -> AuthContext:
    return AuthContext(authenticated=True, granted_scopes=("kingdee-read",))


@pytest.fixture
def material_request() -> Mapping[str, Any]:
    return {
        "request_id": "request-gate5-synthetic-0001",
        "site_id": "gbos.localhost",
        "account_set_ref": "account-set-synthetic-gate5",
        "processing_purpose": "governed_metric_material_lookup",
        "logical_object": "material",
        "limit": 2,
        "offset": 0,
        "timeout_ms": 1_000,
    }
