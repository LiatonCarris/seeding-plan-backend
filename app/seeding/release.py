"""Spend-capped production activation with emergency pause and relock."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, Tuple

from .errors import SeedingError
from .juguang import JuguangClient


class ReleaseSafetyAdapter(Protocol):
    def preflight(
        self,
        *,
        advertiser_id: int,
        campaign_ids: Sequence[int],
        spend_cap_fen: int,
        spend_cap_period: str,
        monitor_source: str,
    ) -> None: ...

    def arm_spend_cap(
        self,
        *,
        advertiser_id: int,
        campaign_ids: Sequence[int],
        spend_cap_fen: int,
        spend_cap_period: str,
    ) -> str: ...

    def verify_armed(self, receipt_id: str) -> bool: ...

    def current_spend_fen(
        self, *, advertiser_id: int, campaign_ids: Sequence[int], period: str
    ) -> int: ...

    def emergency_relock(
        self, *, advertiser_id: int, campaign_ids: Sequence[int]
    ) -> str: ...

    def verify_locked(self, receipt_id: str) -> bool: ...


class DenyAllReleaseSafetyAdapter:
    def preflight(self, **_: object) -> None:
        raise SeedingError(
            "RELEASE_SAFETY_ADAPTER_MISSING",
            "a spend monitor and emergency relock adapter is required",
            status_code=503,
        )

    def arm_spend_cap(self, **_: object) -> str:
        self.preflight()
        raise AssertionError("unreachable")

    def verify_armed(self, receipt_id: str) -> bool:
        self.preflight()
        return False

    def current_spend_fen(self, **_: object) -> int:
        self.preflight()
        return 0

    def emergency_relock(self, **_: object) -> str:
        self.preflight()
        raise AssertionError("unreachable")

    def verify_locked(self, receipt_id: str) -> bool:
        self.preflight()
        return False


@dataclass(frozen=True)
class ReleaseReceipt:
    status: str
    advertiser_id: int
    campaign_ids: Tuple[int, ...]
    spend_cap_fen: int
    spend_cap_period: str
    safety_receipt_id: str | None
    platform_enabled_ids: Tuple[int, ...]
    readback_enabled: bool
    emergency_relock_receipt_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class SpendEvaluationReceipt:
    status: str
    advertiser_id: int
    campaign_ids: Tuple[int, ...]
    observed_spend_fen: int
    spend_cap_fen: int
    emergency_relock_receipt_id: str | None = None


class ProductionReleaseEngine:
    def __init__(self, *, client: JuguangClient, safety: ReleaseSafetyAdapter) -> None:
        self.client = client
        self.safety = safety

    def activate(
        self,
        *,
        advertiser_id: int,
        campaign_ids: Sequence[int],
        spend_cap_fen: int,
        spend_cap_period: str,
        monitor_source: str,
    ) -> ReleaseReceipt:
        exact_ids = tuple(int(value) for value in campaign_ids)
        if not exact_ids or any(value <= 0 for value in exact_ids):
            raise ValueError("positive campaign_ids are required")
        self.safety.preflight(
            advertiser_id=advertiser_id,
            campaign_ids=exact_ids,
            spend_cap_fen=spend_cap_fen,
            spend_cap_period=spend_cap_period,
            monitor_source=monitor_source,
        )
        safety_receipt = self.safety.arm_spend_cap(
            advertiser_id=advertiser_id,
            campaign_ids=exact_ids,
            spend_cap_fen=spend_cap_fen,
            spend_cap_period=spend_cap_period,
        )
        if not self.safety.verify_armed(safety_receipt):
            raise SeedingError(
                "SPEND_CAP_NOT_ARMED",
                "production campaigns cannot be enabled before the spend cap is armed",
                status_code=423,
            )
        try:
            enabled = self.client.update_campaign_status(
                advertiser_id=advertiser_id,
                campaign_ids=exact_ids,
                action_type=1,
                environment="PRODUCTION",
            )
            if set(enabled) != set(exact_ids):
                raise SeedingError(
                    "ENABLE_RESPONSE_MISSING_OBJECT",
                    "Juguang did not acknowledge every exact campaign id",
                )
            campaigns = self.client.list_campaigns(
                advertiser_id=advertiser_id, campaign_ids=exact_ids
            ).base_campaign_dtos
            actual = {int(item.get("campaign_id", 0)): item for item in campaigns}
            readback_enabled = all(
                campaign_id in actual
                and actual[campaign_id].get("campaign_enable") == 1
                and actual[campaign_id].get("campaign_filter_state") != 2
                for campaign_id in exact_ids
            )
            if not readback_enabled:
                raise SeedingError(
                    "RELEASE_READBACK_MISMATCH",
                    "enabled state could not be verified for every campaign",
                )
            return ReleaseReceipt(
                status="RELEASE_ACTIVE",
                advertiser_id=advertiser_id,
                campaign_ids=exact_ids,
                spend_cap_fen=spend_cap_fen,
                spend_cap_period=spend_cap_period,
                safety_receipt_id=safety_receipt,
                platform_enabled_ids=tuple(enabled),
                readback_enabled=True,
            )
        except Exception as exc:
            error_code = (
                exc.code if isinstance(exc, SeedingError) else "RELEASE_INTERNAL_ERROR"
            )
            return self._emergency_pause(
                advertiser_id=advertiser_id,
                campaign_ids=exact_ids,
                spend_cap_fen=spend_cap_fen,
                spend_cap_period=spend_cap_period,
                safety_receipt_id=safety_receipt,
                error_code=error_code,
            )

    def evaluate_spend(
        self,
        *,
        advertiser_id: int,
        campaign_ids: Sequence[int],
        spend_cap_fen: int,
        spend_cap_period: str,
    ) -> SpendEvaluationReceipt:
        exact_ids = tuple(int(value) for value in campaign_ids)
        observed = self.safety.current_spend_fen(
            advertiser_id=advertiser_id,
            campaign_ids=exact_ids,
            period=spend_cap_period,
        )
        if observed <= spend_cap_fen:
            return SpendEvaluationReceipt(
                status="WITHIN_CAP",
                advertiser_id=advertiser_id,
                campaign_ids=exact_ids,
                observed_spend_fen=observed,
                spend_cap_fen=spend_cap_fen,
            )
        pause = self.pause_and_relock(
            advertiser_id=advertiser_id, campaign_ids=exact_ids
        )
        return SpendEvaluationReceipt(
            status="CAP_EXCEEDED_PAUSED" if pause[1] else "CAP_EXCEEDED_RELOCK_FAILED",
            advertiser_id=advertiser_id,
            campaign_ids=exact_ids,
            observed_spend_fen=observed,
            spend_cap_fen=spend_cap_fen,
            emergency_relock_receipt_id=pause[0],
        )

    def pause_and_relock(
        self, *, advertiser_id: int, campaign_ids: Sequence[int]
    ) -> tuple[str | None, bool]:
        exact_ids = tuple(int(value) for value in campaign_ids)
        paused_ok = False
        try:
            paused = self.client.update_campaign_status(
                advertiser_id=advertiser_id,
                campaign_ids=exact_ids,
                action_type=2,
                environment="PRODUCTION",
            )
            paused_ok = set(paused) == set(exact_ids)
        except Exception:
            paused_ok = False
        try:
            receipt = self.safety.emergency_relock(
                advertiser_id=advertiser_id, campaign_ids=exact_ids
            )
            return receipt, bool(paused_ok and self.safety.verify_locked(receipt))
        except Exception:
            return None, False

    def _emergency_pause(
        self,
        *,
        advertiser_id: int,
        campaign_ids: Tuple[int, ...],
        spend_cap_fen: int,
        spend_cap_period: str,
        safety_receipt_id: str,
        error_code: str,
    ) -> ReleaseReceipt:
        relock_receipt, locked = self.pause_and_relock(
            advertiser_id=advertiser_id, campaign_ids=campaign_ids
        )
        return ReleaseReceipt(
            status="RELEASE_FAILED_SAFE" if locked else "RELEASE_EMERGENCY",
            advertiser_id=advertiser_id,
            campaign_ids=campaign_ids,
            spend_cap_fen=spend_cap_fen,
            spend_cap_period=spend_cap_period,
            safety_receipt_id=safety_receipt_id,
            platform_enabled_ids=tuple(),
            readback_enabled=False,
            emergency_relock_receipt_id=relock_receipt,
            error_code=error_code,
        )
