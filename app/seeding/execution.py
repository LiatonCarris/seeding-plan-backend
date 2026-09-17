"""Fail-closed test execution: create, pause, exact readback, and relock."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Literal, Mapping, Protocol, Sequence, Tuple

from .errors import SeedingError
from .identity import sha256_json
from .juguang import JuguangClient


class AccountLockAdapter(Protocol):
    def preflight(self, advertiser_id: int) -> None: ...

    def relock(self, advertiser_id: int) -> str: ...

    def verify_locked(self, advertiser_id: int) -> bool: ...


class DenyAllAccountLockAdapter:
    def preflight(self, advertiser_id: int) -> None:
        raise SeedingError(
            "ACCOUNT_RELOCK_ADAPTER_MISSING",
            "an account relock adapter is required before any platform write",
            status_code=503,
            details={"advertiser_id": advertiser_id},
        )

    def relock(self, advertiser_id: int) -> str:
        self.preflight(advertiser_id)
        raise AssertionError("unreachable")

    def verify_locked(self, advertiser_id: int) -> bool:
        self.preflight(advertiser_id)
        return False


@dataclass(frozen=True)
class ExecutionPlanInput:
    plan_revision_id: str
    logical_plan_key: str
    payload: Mapping[str, Any]
    payload_sha256: str


@dataclass(frozen=True)
class ObjectIds:
    campaign_id: int
    unit_id: int
    creativity_ids: Tuple[int, ...]


@dataclass(frozen=True)
class PlanReadbackReceipt:
    plan_revision_id: str
    logical_plan_key: str
    platform_request_sha256: str
    object_ids: ObjectIds | None
    expected_sha256: str
    actual_sha256: str | None
    pause_verified: bool
    readback_verified: bool
    diff: Tuple[str, ...]
    platform_error_code: str | None = None


@dataclass(frozen=True)
class BatchExecutionReceipt:
    status: str
    advertiser_id: int
    plan_receipts: Tuple[PlanReadbackReceipt, ...]
    relock_receipt_id: str | None
    lock_verified: bool


class TestExecutionEngine:
    def __init__(
        self,
        *,
        client: JuguangClient,
        account_lock: AccountLockAdapter,
        environment: Literal["TEST", "PRODUCTION"] = "TEST",
    ) -> None:
        self.client = client
        self.account_lock = account_lock
        self.environment = environment

    def execute(
        self,
        *,
        advertiser_id: int,
        plans: Sequence[ExecutionPlanInput],
    ) -> BatchExecutionReceipt:
        if not plans:
            raise ValueError("at least one compiled plan is required")
        self.account_lock.preflight(advertiser_id)
        receipts: List[PlanReadbackReceipt] = []
        status = "VERIFIED_PAUSED"
        platform_write_attempted = False

        for plan in plans:
            if sha256_json(plan.payload) != plan.payload_sha256:
                raise SeedingError(
                    "PAYLOAD_SHA_MISMATCH",
                    "compiled platform payload changed before execution",
                    status_code=409,
                    details={"plan_revision_id": plan.plan_revision_id},
                )
            try:
                # Treat the operation as externally indeterminate as soon as the
                # request is allowed to leave the process.  A transport timeout can
                # happen after Juguang committed the create, so the account must be
                # relocked and the object set reconciled even without a response.
                platform_write_attempted = True
                result = self.client.create_cascade(
                    advertiser_id=advertiser_id,
                    payload=plan.payload,
                    environment=self.environment,
                )
            except SeedingError as exc:
                status = (
                    "RECONCILE_REQUIRED"
                    if exc.code == "PLATFORM_TRANSPORT_ERROR"
                    else "FAILED_TERMINAL"
                )
                receipts.append(self._error_receipt(plan, platform_error_code=exc.code))
                break
            except Exception:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan, platform_error_code="UNEXPECTED_CREATE_ERROR"
                    )
                )
                break

            try:
                object_ids = self._extract_object_ids(result.info_list)
            except SeedingError as exc:
                status = "RECONCILE_REQUIRED"
                receipts.append(self._error_receipt(plan, platform_error_code=exc.code))
                break
            except Exception:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan, platform_error_code="UNEXPECTED_CREATE_RESPONSE_ERROR"
                    )
                )
                break

            try:
                paused = self.client.update_campaign_status(
                    advertiser_id=advertiser_id,
                    campaign_ids=(object_ids.campaign_id,),
                    action_type=2,
                    environment=self.environment,
                )
            except SeedingError as exc:
                status = "PAUSE_FAILED_EMERGENCY"
                receipts.append(
                    self._error_receipt(
                        plan,
                        object_ids=object_ids,
                        platform_error_code=exc.code,
                    )
                )
                break
            except Exception:
                status = "PAUSE_FAILED_EMERGENCY"
                receipts.append(
                    self._error_receipt(
                        plan,
                        object_ids=object_ids,
                        platform_error_code="UNEXPECTED_PAUSE_ERROR",
                    )
                )
                break
            if object_ids.campaign_id not in paused:
                status = "PAUSE_FAILED_EMERGENCY"
                receipts.append(
                    self._error_receipt(
                        plan,
                        object_ids=object_ids,
                        platform_error_code="PAUSE_RESPONSE_MISSING_OBJECT",
                    )
                )
                break

            try:
                receipt = self._readback(
                    advertiser_id=advertiser_id,
                    plan=plan,
                    object_ids=object_ids,
                )
            except Exception:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan,
                        object_ids=object_ids,
                        platform_error_code="UNEXPECTED_READBACK_ERROR",
                    )
                )
                break
            receipts.append(receipt)
            if not receipt.readback_verified:
                status = "READBACK_MISMATCH"
                break

        relock_receipt_id: str | None = None
        lock_verified = False
        if platform_write_attempted:
            try:
                relock_receipt_id = self.account_lock.relock(advertiser_id)
                lock_verified = self.account_lock.verify_locked(advertiser_id)
            except Exception:
                status = "RELOCK_FAILED"
            if not lock_verified:
                status = "RELOCK_FAILED"
        return BatchExecutionReceipt(
            status=status,
            advertiser_id=advertiser_id,
            plan_receipts=tuple(receipts),
            relock_receipt_id=relock_receipt_id,
            lock_verified=lock_verified,
        )

    def reconcile(
        self,
        *,
        advertiser_id: int,
        plans: Sequence[ExecutionPlanInput],
    ) -> BatchExecutionReceipt:
        """Resolve an indeterminate create without ever issuing another create."""

        if not plans:
            raise ValueError("at least one compiled plan is required")
        self.account_lock.preflight(advertiser_id)
        receipts: List[PlanReadbackReceipt] = []
        status = "RECONCILED_NOT_CREATED"
        found_any = False

        for plan in plans:
            if sha256_json(plan.payload) != plan.payload_sha256:
                raise SeedingError(
                    "PAYLOAD_SHA_MISMATCH",
                    "compiled platform payload changed before reconciliation",
                    status_code=409,
                )
            expected = plan.payload["create_cascade_info_list"][0]
            expected_campaign = expected["campaign"]
            expected_unit = expected["unit_with_creative_list"][0]["unit"]
            expected_creatives = expected["unit_with_creative_list"][0][
                "creativity_list"
            ]
            try:
                campaigns = self.client.list_campaigns(
                    advertiser_id=advertiser_id,
                    campaign_name=expected_campaign["campaign_name"],
                ).base_campaign_dtos
            except SeedingError as exc:
                status = "RECONCILE_REQUIRED"
                receipts.append(self._error_receipt(plan, platform_error_code=exc.code))
                break
            except Exception:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan, platform_error_code="UNEXPECTED_CAMPAIGN_DISCOVERY_ERROR"
                    )
                )
                break
            exact_campaigns = tuple(
                item
                for item in campaigns
                if item.get("campaign_name") == expected_campaign["campaign_name"]
            )
            if not exact_campaigns:
                receipts.append(
                    PlanReadbackReceipt(
                        plan_revision_id=plan.plan_revision_id,
                        logical_plan_key=plan.logical_plan_key,
                        platform_request_sha256=plan.payload_sha256,
                        object_ids=None,
                        expected_sha256=plan.payload_sha256,
                        actual_sha256=sha256_json({"campaigns": []}),
                        pause_verified=True,
                        readback_verified=True,
                        diff=("OBJECT_NOT_CREATED",),
                    )
                )
                continue
            if len(exact_campaigns) != 1:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan, platform_error_code="CAMPAIGN_DISCOVERY_AMBIGUOUS"
                    )
                )
                break
            try:
                campaign_id = int(exact_campaigns[0].get("campaign_id", 0))
            except (TypeError, ValueError):
                campaign_id = 0
            if campaign_id <= 0:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan, platform_error_code="CAMPAIGN_DISCOVERY_ID_MISSING"
                    )
                )
                break
            try:
                units = self.client.list_units(
                    advertiser_id=advertiser_id, campaign_id=campaign_id
                ).unit_infos
            except SeedingError as exc:
                status = "RECONCILE_REQUIRED"
                receipts.append(self._error_receipt(plan, platform_error_code=exc.code))
                break
            except Exception:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan, platform_error_code="UNEXPECTED_UNIT_DISCOVERY_ERROR"
                    )
                )
                break
            exact_units = tuple(
                item
                for item in units
                if (item.get("name") or item.get("unit_name"))
                == expected_unit["unit_name"]
            )
            if len(exact_units) != 1:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan, platform_error_code="UNIT_DISCOVERY_AMBIGUOUS"
                    )
                )
                break
            try:
                unit_id = int(exact_units[0].get("unit_id", 0))
            except (TypeError, ValueError):
                unit_id = 0
            try:
                creativity_rows = self.client.search_creativities(
                    advertiser_id=advertiser_id,
                    campaign_id=campaign_id,
                    unit_id=unit_id,
                ).creativity_dtos
            except SeedingError as exc:
                status = "RECONCILE_REQUIRED"
                receipts.append(self._error_receipt(plan, platform_error_code=exc.code))
                break
            except Exception:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan,
                        platform_error_code="UNEXPECTED_CREATIVITY_DISCOVERY_ERROR",
                    )
                )
                break
            expected_note_ids = {item.get("note_id") for item in expected_creatives}
            exact_creatives = tuple(
                item
                for item in creativity_rows
                if item.get("note_id") in expected_note_ids
            )
            try:
                creativity_ids = tuple(
                    int(item.get("creativity_id", 0)) for item in exact_creatives
                )
            except (TypeError, ValueError):
                creativity_ids = tuple()
            if (
                unit_id <= 0
                or len(creativity_ids) != len(expected_creatives)
                or any(value <= 0 for value in creativity_ids)
            ):
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan, platform_error_code="CREATIVITY_DISCOVERY_AMBIGUOUS"
                    )
                )
                break
            object_ids = ObjectIds(campaign_id, unit_id, creativity_ids)
            found_any = True
            try:
                paused = self.client.update_campaign_status(
                    advertiser_id=advertiser_id,
                    campaign_ids=(campaign_id,),
                    action_type=2,
                    environment=self.environment,
                )
            except SeedingError as exc:
                status = "PAUSE_FAILED_EMERGENCY"
                receipts.append(
                    self._error_receipt(
                        plan, object_ids=object_ids, platform_error_code=exc.code
                    )
                )
                break
            except Exception:
                status = "PAUSE_FAILED_EMERGENCY"
                receipts.append(
                    self._error_receipt(
                        plan,
                        object_ids=object_ids,
                        platform_error_code="UNEXPECTED_PAUSE_ERROR",
                    )
                )
                break
            if campaign_id not in paused:
                status = "PAUSE_FAILED_EMERGENCY"
                receipts.append(
                    self._error_receipt(
                        plan,
                        object_ids=object_ids,
                        platform_error_code="PAUSE_RESPONSE_MISSING_OBJECT",
                    )
                )
                break
            try:
                receipt = self._readback(
                    advertiser_id=advertiser_id, plan=plan, object_ids=object_ids
                )
            except Exception:
                status = "RECOVERY_REQUIRED"
                receipts.append(
                    self._error_receipt(
                        plan,
                        object_ids=object_ids,
                        platform_error_code="UNEXPECTED_READBACK_ERROR",
                    )
                )
                break
            receipts.append(receipt)
            if not receipt.readback_verified:
                status = "READBACK_MISMATCH"
                break

        if found_any and status == "RECONCILED_NOT_CREATED":
            status = "RECONCILED_VERIFIED_PAUSED"

        relock_receipt_id: str | None = None
        lock_verified = False
        # Reconciliation is itself a safety operation. Relock even when no
        # object was found because the earlier write outcome was indeterminate.
        try:
            relock_receipt_id = self.account_lock.relock(advertiser_id)
            lock_verified = self.account_lock.verify_locked(advertiser_id)
        except Exception:
            status = "RELOCK_FAILED"
        if not lock_verified:
            status = "RELOCK_FAILED"
        return BatchExecutionReceipt(
            status=status,
            advertiser_id=advertiser_id,
            plan_receipts=tuple(receipts),
            relock_receipt_id=relock_receipt_id,
            lock_verified=lock_verified,
        )

    @staticmethod
    def _extract_object_ids(rows: Sequence[Mapping[str, Any]]) -> ObjectIds:
        if len(rows) != 1:
            raise SeedingError(
                "PLATFORM_CREATE_RESPONSE_AMBIGUOUS",
                "expected exactly one cascade result",
            )
        row = rows[0]
        campaign_id = int(row.get("campaign", {}).get("campaign_id", 0))
        units = row.get("unit_with_creative_list", [])
        if campaign_id <= 0 or len(units) != 1:
            raise SeedingError(
                "PLATFORM_CREATE_RESPONSE_AMBIGUOUS",
                "create response did not contain one campaign and one unit",
            )
        unit = units[0]
        unit_id = int(unit.get("unit", {}).get("unit_id", 0))
        creativity_ids = tuple(
            int(item.get("creativity_id", 0))
            for item in unit.get("creativity_list", [])
        )
        if (
            unit_id <= 0
            or not creativity_ids
            or any(value <= 0 for value in creativity_ids)
        ):
            raise SeedingError(
                "PLATFORM_CREATE_RESPONSE_AMBIGUOUS",
                "create response omitted unit or creativity ids",
            )
        return ObjectIds(
            campaign_id=campaign_id,
            unit_id=unit_id,
            creativity_ids=creativity_ids,
        )

    def _readback(
        self,
        *,
        advertiser_id: int,
        plan: ExecutionPlanInput,
        object_ids: ObjectIds,
    ) -> PlanReadbackReceipt:
        expected = plan.payload["create_cascade_info_list"][0]
        expected_campaign = expected["campaign"]
        expected_unit_group = expected["unit_with_creative_list"][0]
        expected_unit = expected_unit_group["unit"]
        expected_creatives = expected_unit_group["creativity_list"]

        try:
            campaigns = self.client.list_campaigns(
                advertiser_id=advertiser_id,
                campaign_ids=(object_ids.campaign_id,),
            ).base_campaign_dtos
            units = self.client.list_units(
                advertiser_id=advertiser_id,
                unit_ids=(object_ids.unit_id,),
            ).unit_infos
            creatives = self.client.search_creativities(
                advertiser_id=advertiser_id,
                creativity_ids=object_ids.creativity_ids,
            ).creativity_dtos
        except SeedingError as exc:
            return self._error_receipt(
                plan,
                object_ids=object_ids,
                platform_error_code=exc.code,
            )

        diff: List[str] = []
        if len(campaigns) != 1:
            diff.append("CAMPAIGN_READBACK_COUNT_MISMATCH")
            campaign = {}
        else:
            campaign = campaigns[0]
        if len(units) != 1:
            diff.append("UNIT_READBACK_COUNT_MISMATCH")
            unit = {}
        else:
            unit = units[0]
        if len(creatives) != len(expected_creatives):
            diff.append("CREATIVITY_READBACK_COUNT_MISMATCH")

        self._compare(
            diff,
            "CAMPAIGN_NAME",
            expected_campaign.get("campaign_name"),
            campaign.get("campaign_name"),
        )
        self._compare(
            diff,
            "MARKETING_TARGET",
            expected_campaign.get("marketing_target"),
            campaign.get("marketing_target"),
        )
        self._compare(
            diff,
            "PLACEMENT",
            expected_campaign.get("placement"),
            campaign.get("placement"),
        )
        self._compare(
            diff,
            "BIDDING_STRATEGY",
            expected_campaign.get("bidding_strategy"),
            campaign.get("bidding_strategy"),
        )
        self._compare(
            diff,
            "CAMPAIGN_BUDGET",
            expected_campaign.get("origin_campaign_day_budget"),
            campaign.get("campaign_day_budget"),
        )
        self._compare(
            diff, "UNIT_NAME", expected_unit.get("unit_name"), unit.get("name")
        )
        self._compare(
            diff,
            "TARGET_TYPE",
            expected_unit.get("target_type"),
            unit.get("target_type"),
        )
        self._compare(
            diff, "EVENT_BID", expected_unit.get("event_bid"), unit.get("event_bid")
        )
        self._compare_target_info(
            diff, expected_unit.get("target_info", {}), unit.get("target_config", {})
        )
        self._compare_keywords(
            diff,
            expected_unit.get("keyword_with_bid", []),
            unit.get("keyword_with_bids", []),
        )

        actual_creatives = {
            int(item.get("creativity_id", 0)): item for item in creatives
        }
        for index, creativity_id in enumerate(object_ids.creativity_ids):
            actual = actual_creatives.get(creativity_id)
            if actual is None:
                diff.append(f"CREATIVITY_{creativity_id}_MISSING")
                continue
            expected_creative = expected_creatives[index]
            self._compare(
                diff,
                f"CREATIVITY_{creativity_id}_NOTE",
                expected_creative.get("note_id"),
                actual.get("note_id"),
            )
            self._compare(
                diff,
                f"CREATIVITY_{creativity_id}_CONVERSION",
                expected_creative.get("conversion_type"),
                actual.get("conversion_type"),
            )

        campaign_paused = (
            campaign.get("campaign_filter_state") == 2
            or campaign.get("campaign_enable") == 0
        )
        unit_paused = unit.get("enable") == 0 or unit.get("unit_filter_state") in {4, 6}
        creativity_paused = all(
            item.get("creativity_enable") == 0
            or item.get("creativity_filter_state") in {3, 4, 5}
            for item in creatives
        )
        pause_verified = bool(campaign_paused and unit_paused and creativity_paused)
        if not pause_verified:
            diff.append("PAUSE_STATE_NOT_VERIFIED")
        actual = {
            "campaigns": campaigns,
            "units": units,
            "creativities": creatives,
        }
        return PlanReadbackReceipt(
            plan_revision_id=plan.plan_revision_id,
            logical_plan_key=plan.logical_plan_key,
            platform_request_sha256=sha256_json(plan.payload),
            object_ids=object_ids,
            expected_sha256=plan.payload_sha256,
            actual_sha256=sha256_json(actual),
            pause_verified=pause_verified,
            readback_verified=not diff,
            diff=tuple(diff),
        )

    @staticmethod
    def _compare(diff: List[str], label: str, expected: Any, actual: Any) -> None:
        if expected != actual:
            diff.append(f"{label}_MISMATCH")

    @staticmethod
    def _compare_target_info(
        diff: List[str], expected: Mapping[str, Any], actual: Mapping[str, Any]
    ) -> None:
        for field in (
            "target_gender",
            "target_area_code",
            "target_age",
            "target_device",
        ):
            if expected.get(field) != actual.get(field):
                diff.append(f"TARGET_{field.upper()}_MISMATCH")
        expected_crowds = {
            item.get("value")
            for item in expected.get("crowd_target", {}).get("crowd_pkg", [])
        }
        actual_crowds = {
            item.get("value")
            for item in actual.get("crowd_target", {}).get("crowd_pkg", [])
        }
        if expected_crowds != actual_crowds:
            diff.append("TARGET_CROWD_PACKAGES_MISMATCH")
        if tuple(expected.get("keywords", [])) != tuple(actual.get("keywords", [])):
            diff.append("TARGET_BEHAVIOR_KEYWORDS_MISMATCH")

    @staticmethod
    def _compare_keywords(
        diff: List[str],
        expected: Sequence[Mapping[str, Any]],
        actual: Sequence[Mapping[str, Any]],
    ) -> None:
        def normalized(rows: Sequence[Mapping[str, Any]]) -> List[Tuple[Any, Any, Any]]:
            return sorted(
                (
                    item.get("keyword"),
                    item.get("bid"),
                    item.get("phrase_match_type"),
                )
                for item in rows
            )

        if normalized(expected) != normalized(actual):
            diff.append("KEYWORD_WITH_BID_MISMATCH")

    @staticmethod
    def _error_receipt(
        plan: ExecutionPlanInput,
        *,
        object_ids: ObjectIds | None = None,
        platform_error_code: str,
    ) -> PlanReadbackReceipt:
        return PlanReadbackReceipt(
            plan_revision_id=plan.plan_revision_id,
            logical_plan_key=plan.logical_plan_key,
            platform_request_sha256=sha256_json(plan.payload),
            object_ids=object_ids,
            expected_sha256=plan.payload_sha256,
            actual_sha256=None,
            pause_verified=False,
            readback_verified=False,
            diff=(platform_error_code,),
            platform_error_code=platform_error_code,
        )
