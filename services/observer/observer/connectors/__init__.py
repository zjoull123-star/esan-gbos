"""Provider-neutral connector contracts for the local channel pilot."""

from .serialization import (
    CONNECTOR_CHANNELS,
    canonical_observation_event_v11,
    channel_for_connector,
)

__all__ = [
    "CONNECTOR_CHANNELS",
    "canonical_observation_event_v11",
    "channel_for_connector",
]
