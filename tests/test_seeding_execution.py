from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pytest

from app.seeding.errors import SeedingError
from app.seeding.execution import (
    DenyAllAccountLockAdapter,
    ExecutionPlanInput,
    TestExecutionEngine as ExecutionEngine,
)
from app.seeding.identity import sha256_json
from app.seeding.juguang import (
    JuguangCampaignList,
    JuguangCreateResult,
    JuguangCreativityList,
    JuguangUnitList,
)


def payload() -> dict[str, Any]:
    return {
        "advertiser_id": 123,
        "create_type": 1,
        "create_cascade_info_list": [
            {
                "campaign": {
                    "campaign_name": "seed-campaign",
                    "marketing_target": 4,
                    "placement": 1,
                    "bidding_strategy": 2,
                    "origin_campaign_day_budget": 10000,
                },
                "unit_with_creative_list": [
                    {
                        "unit": {
                            "unit_name": "seed-unit",
                            "target_type": 3,
                            "event_bid": 500,
                            "target_info": {
                                "target_gender": "all",
                                "target_area_code": "-1",
                                "target_age": "all",
                                "target_device": "all",
                                "crowd_target": {"crowd_pkg": [{"value": "pkg-1"}]},
                                "keywords": [],
                            },
                            "keyword_with_bid": [],
                        },
                        "creativity_list": [
                            {"note_id": "note-1", "conversion_type": 0}
                        ],
                    }
                ],
            }
        ],
    }


def plan() -> ExecutionPlanInput:
    body = payload()
    return ExecutionPlanInput(
        plan_revision_id="planrev-1",
        logical_plan_key="a" * 64,
        payload=body,
        payload_sha256=sha256_json(body),
    )


@dataclass
class FakeLock:
    verified: bool = True
    preflight_calls: int = 0
    relock_calls: int = 0

    def preflight(self, advertiser_id: int) -> None:
        assert advertiser_id == 123
        self.preflight_calls += 1

    def relock(self, advertiser_id: int) -> str:
        assert advertiser_id == 123
        self.relock_calls += 1
        return "lock-receipt-1"

    def verify_locked(self, advertiser_id: int) -> bool:
        assert advertiser_id == 123
        return self.verified


class FakeClient:
    def __init__(
        self,
        *,
        create_error: SeedingError | None = None,
        pause_error: SeedingError | None = None,
        readback_campaign_name: str = "seed-campaign",
    ) -> None:
        self.create_error = create_error
        self.pause_error = pause_error
        self.readback_campaign_name = readback_campaign_name

    def create_cascade(
        self, *, advertiser_id: int, payload: Mapping[str, Any], environment: str
    ) -> JuguangCreateResult:
        assert advertiser_id == 123
        assert environment == "TEST"
        if self.create_error:
            raise self.create_error
        return JuguangCreateResult(
            info_list=(
                {
                    "campaign": {"campaign_id": 1001},
                    "unit_with_creative_list": [
                        {
                            "unit": {"unit_id": 2001},
                            "creativity_list": [{"creativity_id": 3001}],
                        }
                    ],
                },
            )
        )

    def update_campaign_status(
        self,
        *,
        advertiser_id: int,
        campaign_ids: Sequence[int],
        action_type: int,
        environment: str,
    ) -> tuple[int, ...]:
        assert (advertiser_id, tuple(campaign_ids), action_type) == (123, (1001,), 2)
        assert environment == "TEST"
        if self.pause_error:
            raise self.pause_error
        return (1001,)

    def list_campaigns(self, **_: Any) -> JuguangCampaignList:
        return JuguangCampaignList(
            base_campaign_dtos=(
                {
                    "campaign_id": 1001,
                    "campaign_name": self.readback_campaign_name,
                    "marketing_target": 4,
                    "placement": 1,
                    "bidding_strategy": 2,
                    "campaign_day_budget": 10000,
                    "campaign_filter_state": 2,
                },
            )
        )

    def list_units(self, **_: Any) -> JuguangUnitList:
        return JuguangUnitList(
            unit_infos=(
                {
                    "unit_id": 2001,
                    "name": "seed-unit",
                    "target_type": 3,
                    "event_bid": 500,
                    "enable": 0,
                    "target_config": {
                        "target_gender": "all",
                        "target_area_code": "-1",
                        "target_age": "all",
                        "target_device": "all",
                        "crowd_target": {"crowd_pkg": [{"value": "pkg-1"}]},
                        "keywords": [],
                    },
                    "keyword_with_bids": [],
                },
            )
        )

    def search_creativities(self, **_: Any) -> JuguangCreativityList:
        return JuguangCreativityList(
            creativity_dtos=(
                {
                    "creativity_id": 3001,
                    "note_id": "note-1",
                    "conversion_type": 0,
                    "creativity_filter_state": 3,
                },
            )
        )


def test_create_pause_readback_and_relock_happy_path() -> None:
    lock = FakeLock()
    result = ExecutionEngine(client=FakeClient(), account_lock=lock).execute(
        advertiser_id=123, plans=(plan(),)
    )
    assert result.status == "VERIFIED_PAUSED"
    assert result.lock_verified is True
    assert result.relock_receipt_id == "lock-receipt-1"
    assert result.plan_receipts[0].readback_verified is True
    assert lock.preflight_calls == 1
    assert lock.relock_calls == 1


def test_create_transport_error_requires_reconcile_and_relock() -> None:
    lock = FakeLock()
    client = FakeClient(
        create_error=SeedingError("PLATFORM_TRANSPORT_ERROR", "timeout")
    )
    result = ExecutionEngine(client=client, account_lock=lock).execute(
        advertiser_id=123, plans=(plan(),)
    )
    assert result.status == "RECONCILE_REQUIRED"
    assert result.lock_verified is True
    assert lock.relock_calls == 1


def test_platform_business_rejection_is_terminal_but_still_relocks() -> None:
    lock = FakeLock()
    client = FakeClient(
        create_error=SeedingError("PLATFORM_BUSINESS_ERROR", "rejected")
    )
    result = ExecutionEngine(client=client, account_lock=lock).execute(
        advertiser_id=123, plans=(plan(),)
    )
    assert result.status == "FAILED_TERMINAL"
    assert result.lock_verified is True


def test_unexpected_create_error_fails_closed_and_still_relocks() -> None:
    class UnexpectedCreateClient(FakeClient):
        def create_cascade(self, **_: Any) -> JuguangCreateResult:
            raise RuntimeError("unexpected adapter failure")

    lock = FakeLock()
    result = ExecutionEngine(
        client=UnexpectedCreateClient(), account_lock=lock
    ).execute(advertiser_id=123, plans=(plan(),))
    assert result.status == "RECOVERY_REQUIRED"
    assert result.plan_receipts[0].diff == ("UNEXPECTED_CREATE_ERROR",)
    assert result.lock_verified is True
    assert lock.relock_calls == 1


def test_pause_failure_is_emergency_and_relocks() -> None:
    lock = FakeLock()
    client = FakeClient(pause_error=SeedingError("PLATFORM_TRANSPORT_ERROR", "timeout"))
    result = ExecutionEngine(client=client, account_lock=lock).execute(
        advertiser_id=123, plans=(plan(),)
    )
    assert result.status == "PAUSE_FAILED_EMERGENCY"
    assert result.lock_verified is True


def test_readback_mismatch_fails_closed_and_relocks() -> None:
    lock = FakeLock()
    result = ExecutionEngine(
        client=FakeClient(readback_campaign_name="wrong"), account_lock=lock
    ).execute(advertiser_id=123, plans=(plan(),))
    assert result.status == "READBACK_MISMATCH"
    assert "CAMPAIGN_NAME_MISMATCH" in result.plan_receipts[0].diff
    assert result.lock_verified is True


def test_missing_account_lock_adapter_blocks_before_platform_write() -> None:
    with pytest.raises(SeedingError) as exc:
        ExecutionEngine(
            client=FakeClient(), account_lock=DenyAllAccountLockAdapter()
        ).execute(advertiser_id=123, plans=(plan(),))
    assert exc.value.code == "ACCOUNT_RELOCK_ADAPTER_MISSING"


def test_reconcile_discovers_existing_object_pauses_and_verifies() -> None:
    lock = FakeLock()
    result = ExecutionEngine(client=FakeClient(), account_lock=lock).reconcile(
        advertiser_id=123, plans=(plan(),)
    )
    assert result.status == "RECONCILED_VERIFIED_PAUSED"
    assert result.plan_receipts[0].object_ids is not None
    assert result.plan_receipts[0].readback_verified is True
    assert result.lock_verified is True


def test_reconcile_verified_absence_never_recreates() -> None:
    class MissingClient(FakeClient):
        def list_campaigns(self, **_: Any) -> JuguangCampaignList:
            return JuguangCampaignList(base_campaign_dtos=tuple())

    lock = FakeLock()
    result = ExecutionEngine(client=MissingClient(), account_lock=lock).reconcile(
        advertiser_id=123, plans=(plan(),)
    )
    assert result.status == "RECONCILED_NOT_CREATED"
    assert result.plan_receipts[0].diff == ("OBJECT_NOT_CREATED",)
    assert lock.relock_calls == 1


def test_reconcile_invalid_platform_id_fails_closed_and_relocks() -> None:
    class InvalidCampaignIdClient(FakeClient):
        def list_campaigns(self, **_: Any) -> JuguangCampaignList:
            return JuguangCampaignList(
                base_campaign_dtos=(
                    {"campaign_id": "invalid", "campaign_name": "seed-campaign"},
                )
            )

    lock = FakeLock()
    result = ExecutionEngine(
        client=InvalidCampaignIdClient(), account_lock=lock
    ).reconcile(advertiser_id=123, plans=(plan(),))
    assert result.status == "RECOVERY_REQUIRED"
    assert result.plan_receipts[0].diff == ("CAMPAIGN_DISCOVERY_ID_MISSING",)
    assert result.lock_verified is True
    assert lock.relock_calls == 1
