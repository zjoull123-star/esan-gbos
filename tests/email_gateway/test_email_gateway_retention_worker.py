from __future__ import annotations

from datetime import UTC, datetime

NOW = datetime(2026, 8, 14, 1, tzinfo=UTC)


def test_cycle_is_default_closed_and_execute_requires_both_opt_ins() -> None:
    from services.local_pilot_runtime.email_gateway_retention_worker import execution_enabled

    assert execution_enabled({}) is False
    assert execution_enabled({"GBOS_EMAIL_GATEWAY_RETENTION_ENABLED": "true"}) is False
    assert execution_enabled(
        {
            "GBOS_EMAIL_GATEWAY_RETENTION_ENABLED": "true",
            "GBOS_EMAIL_GATEWAY_RETENTION_EXECUTE_ACKNOWLEDGED": "true",
            "GBOS_EMAIL_GATEWAY_KILL_SWITCH": "false",
            "GBOS_GLOBAL_KILL_SWITCH": "false",
        }
    ) is True


def test_loop_rechecks_kill_switches_and_recovers_on_next_cycle() -> None:
    from services.local_pilot_runtime.email_gateway_retention_worker import run_loop

    states = iter([False, True, True])
    calls: list[str] = []

    class Stop:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits >= 3

        def wait(self, _timeout):
            self.waits += 1
            return self.is_set()

    def cycle():
        calls.append("cycle")
        if len(calls) == 1:
            raise RuntimeError("injected")
        return 0

    stop = Stop()
    failures = run_loop(
        run_cycle=cycle,
        cycle_allowed=lambda: next(states),
        interval_seconds=60,
        stop_event=stop,
    )

    assert calls == ["cycle", "cycle"]
    assert failures == 1


def test_http_observer_verifier_accepts_only_exact_bound_receipt() -> None:
    from services.email_gateway.models import ContentProjection, TenantScope
    from services.local_pilot_runtime.email_gateway_retention_worker import (
        HttpObserverTombstoneVerifier,
    )

    projection = ContentProjection(
        projection_ref="DRF-01",
        site_id="site.local",
        kind="draft_projection",
        identity_ref=None,
        evidence_ref="EVD-01",
        expires_at=NOW,
        observer_expiration_receipt_ref="TMB-01",
        payload_digest="sha256:" + "a" * 64,
        active_draft_ref=None,
        confirmed=False,
    )

    def transport(**_kwargs):
        return 200, {
            "schema_version": "1.0",
            "site_id": "site.local",
            "evidence_ref": "EVD-01",
            "tombstone_receipt_ref": "TMB-01",
            "verified": True,
        }

    verifier = HttpObserverTombstoneVerifier(
        endpoint="http://observer-api:8003/internal/v1/retention/tombstones/verify",
        bearer_token="internal-token",
        auth_ref="observer-retention-verifier-v1",
        transport=transport,
    )
    assert verifier.verify_tombstone(
        TenantScope("site.local", "sales_follow_up"), projection, now=NOW
    )

    wrong = HttpObserverTombstoneVerifier(
        endpoint="http://observer-api:8003/internal/v1/retention/tombstones/verify",
        bearer_token="internal-token",
        auth_ref="observer-retention-verifier-v1",
        transport=lambda **_kwargs: (200, {"verified": True}),
    )
    assert wrong.verify_tombstone(
        TenantScope("site.local", "sales_follow_up"), projection, now=NOW
    ) is False
