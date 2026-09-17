from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.seeding.juguang import JuguangCampaignList
from app.seeding.release import ProductionReleaseEngine


@dataclass
class FakeSafety:
    spend_fen: int = 0
    armed: bool = True
    locked: bool = True
    relock_calls: int = 0

    def preflight(self, **_: Any) -> None:
        return None

    def arm_spend_cap(self, **_: Any) -> str:
        return "cap-receipt-1"

    def verify_armed(self, receipt_id: str) -> bool:
        assert receipt_id == "cap-receipt-1"
        return self.armed

    def current_spend_fen(self, **_: Any) -> int:
        return self.spend_fen

    def emergency_relock(self, **_: Any) -> str:
        self.relock_calls += 1
        return "relock-receipt-1"

    def verify_locked(self, receipt_id: str) -> bool:
        assert receipt_id == "relock-receipt-1"
        return self.locked


class FakeReleaseClient:
    def __init__(self, *, readback_enabled: bool = True) -> None:
        self.readback_enabled = readback_enabled
        self.actions: list[int] = []

    def update_campaign_status(
        self,
        *,
        advertiser_id: int,
        campaign_ids: Sequence[int],
        action_type: int,
        environment: str,
    ) -> tuple[int, ...]:
        assert advertiser_id == 123
        assert environment == "PRODUCTION"
        self.actions.append(action_type)
        return tuple(campaign_ids)

    def list_campaigns(self, **_: Any) -> JuguangCampaignList:
        return JuguangCampaignList(
            base_campaign_dtos=(
                {
                    "campaign_id": 1001,
                    "campaign_enable": 1 if self.readback_enabled else 0,
                    "campaign_filter_state": 1 if self.readback_enabled else 2,
                },
            )
        )


def test_release_arms_cap_before_enable_and_verifies_readback() -> None:
    client = FakeReleaseClient()
    engine = ProductionReleaseEngine(client=client, safety=FakeSafety())
    result = engine.activate(
        advertiser_id=123,
        campaign_ids=(1001,),
        spend_cap_fen=50_000,
        spend_cap_period="TOTAL",
        monitor_source="juguang-report-v2",
    )
    assert result.status == "RELEASE_ACTIVE"
    assert result.readback_enabled is True
    assert client.actions == [1]


def test_release_readback_mismatch_triggers_pause_and_relock() -> None:
    client = FakeReleaseClient(readback_enabled=False)
    safety = FakeSafety()
    result = ProductionReleaseEngine(client=client, safety=safety).activate(
        advertiser_id=123,
        campaign_ids=(1001,),
        spend_cap_fen=50_000,
        spend_cap_period="TOTAL",
        monitor_source="juguang-report-v2",
    )
    assert result.status == "RELEASE_FAILED_SAFE"
    assert client.actions == [1, 2]
    assert safety.relock_calls == 1


def test_spend_cap_excess_pauses_and_relocks() -> None:
    client = FakeReleaseClient()
    safety = FakeSafety(spend_fen=50_001)
    result = ProductionReleaseEngine(client=client, safety=safety).evaluate_spend(
        advertiser_id=123,
        campaign_ids=(1001,),
        spend_cap_fen=50_000,
        spend_cap_period="TOTAL",
    )
    assert result.status == "CAP_EXCEEDED_PAUSED"
    assert result.observed_spend_fen == 50_001
    assert client.actions == [2]
