"""Compile confirmed SEEDING plans into strict Juguang cascade/create DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import (
    AudienceCandidate,
    DynamicBehaviorTargeting,
    JuguangPlatformProfile,
    KeywordCandidate,
    ProjectConfig,
    StaticDmpSnapshot,
)
from .decision import KeywordDecision
from .errors import SeedingError
from .identity import sha256_json


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WireCrowdPackage(WireModel):
    value: str
    name: str
    group_id: str


class WireCrowdTarget(WireModel):
    crowd_pkg: Tuple[WireCrowdPackage, ...]
    dmp_permission: bool = True


class WireTargetInfo(WireModel):
    target_gender: Literal["all", "0", "1"] = "all"
    target_area_code: str
    target_age: str = "all"
    target_device: Literal["all", "ios", "android"] = "all"
    crowd_target: Optional[WireCrowdTarget] = None
    keywords: Tuple[str, ...] = tuple()
    keyword_target_period: Optional[Literal[3, 7, 15, 30]] = None
    keyword_target_action: Tuple[Literal[1, 2, 3], ...] = tuple()
    intelligent_expansion: Literal[0] = 0

    @model_validator(mode="after")
    def validate_behavior_targeting(self) -> "WireTargetInfo":
        if self.keywords and (
            self.keyword_target_period is None or not self.keyword_target_action
        ):
            raise ValueError("behavior keywords require a period and actions")
        return self


class WireKeywordWithBid(WireModel):
    keyword: str
    bid: int = Field(ge=0)
    keyword_source: int = 0
    phrase_match_type: Literal[0, 1] = 1
    feed_bid: int = Field(default=0, ge=0)


class WireCampaign(WireModel):
    campaign_name: str = Field(min_length=1, max_length=50)
    marketing_target: Literal[4]
    placement: Literal[1, 2]
    delivery_mode: Literal[0, 1]
    promotion_target: Literal[1]
    optimize_objective: int = Field(ge=0)
    deep_optimize_objective: int = Field(ge=-1)
    bidding_strategy: Literal[2, 3, 7]
    time_type: Literal[0] = 0
    time_period_type: Literal[0] = 0
    limit_day_budget: Literal[1] = 1
    origin_campaign_day_budget: int = Field(gt=0)
    pacing_mode: Literal[1, 2] = 1
    smart_switch: Literal[0] = 0
    explore_state: Literal[0] = 0
    search_flag: Literal[0] = 0
    horse_race: Literal[0] = 0


class WireUnit(WireModel):
    unit_name: str = Field(min_length=1, max_length=50)
    keyword_with_bid: Tuple[WireKeywordWithBid, ...] = tuple()
    keyword_gen_type: Literal[0] = 0
    target_type: Literal[0, 2, 3]
    target_info: WireTargetInfo
    event_bid: int = Field(ge=0)


class WireCreative(WireModel):
    creativity_name: str = Field(min_length=1, max_length=50)
    note_id: str = Field(min_length=1, max_length=160)
    conversion_type: int = Field(ge=0)


class WireUnitWithCreative(WireModel):
    unit: WireUnit
    creativity_list: Tuple[WireCreative, ...]


class WireCascade(WireModel):
    campaign: WireCampaign
    unit_with_creative_list: Tuple[WireUnitWithCreative, ...]


class JuguangCreatePayload(WireModel):
    advertiser_id: int = Field(gt=0)
    create_type: Literal[1] = 1
    create_cascade_info_list: Tuple[WireCascade, ...]


@dataclass(frozen=True)
class CompiledJuguangPayload:
    payload: JuguangCreatePayload
    payload_sha256: str
    platform_profile_id: str
    platform_profile_version: int
    verification_proof_sha256: str


def _short(value: str, length: int = 10) -> str:
    cleaned = "".join(character for character in value if character.isalnum())
    return (cleaned or "x")[-length:]


def _require_verified_profile(profile: JuguangPlatformProfile) -> None:
    if not profile.verified or not profile.verification_proof_sha256:
        raise SeedingError(
            "PLATFORM_ENUM_UNVERIFIED",
            "Juguang profile has not been verified in a test account",
        )


def _campaign(
    *,
    project: ProjectConfig,
    plan: Mapping[str, object],
    profile: JuguangPlatformProfile,
    placement: Literal[1, 2],
) -> WireCampaign:
    budget = int(plan["daily_budget_fen"])
    if (
        not profile.minimum_campaign_budget_fen
        <= budget
        <= profile.maximum_campaign_budget_fen
    ):
        raise SeedingError(
            "PLATFORM_BUDGET_OUT_OF_RANGE",
            "plan budget is outside the verified Juguang range",
            details={
                "budget_fen": budget,
                "minimum_fen": profile.minimum_campaign_budget_fen,
                "maximum_fen": profile.maximum_campaign_budget_fen,
            },
        )
    channel = "F" if placement == 1 else "S"
    name = f"SEED-{_short(project.project_id, 8)}-{channel}-{_short(str(plan['note_id']), 8)}"
    return WireCampaign(
        campaign_name=name,
        marketing_target=profile.marketing_target,
        placement=placement,
        delivery_mode=profile.delivery_mode,
        promotion_target=profile.promotion_target,
        optimize_objective=profile.optimize_objective,
        deep_optimize_objective=profile.deep_optimize_objective,
        bidding_strategy=profile.bidding_strategy,
        origin_campaign_day_budget=budget,
    )


def _target_info_for_audience(
    audience: AudienceCandidate,
    *,
    profile: JuguangPlatformProfile,
    keywords_by_id: Mapping[str, KeywordCandidate],
) -> WireTargetInfo:
    common: Dict[str, object] = {
        "target_gender": profile.target_gender,
        "target_area_code": profile.target_area_code,
        "target_age": profile.target_age,
        "target_device": profile.target_device,
    }
    if isinstance(audience.asset, StaticDmpSnapshot):
        return WireTargetInfo(
            **common,
            crowd_target=WireCrowdTarget(
                crowd_pkg=(
                    WireCrowdPackage(
                        value=audience.asset.package_id,
                        name=audience.asset.package_name,
                        group_id=audience.asset.group_id,
                    ),
                )
            ),
        )
    if not isinstance(audience.asset, DynamicBehaviorTargeting):
        raise SeedingError("UNSUPPORTED_AUDIENCE_MODE", "unknown audience asset")
    missing = [
        keyword_id
        for keyword_id in audience.asset.keyword_ids
        if keyword_id not in keywords_by_id
    ]
    if missing:
        raise SeedingError(
            "SOURCE_PROOF_MISSING",
            "dynamic audience keywords cannot be resolved",
            details={"missing_keyword_ids": missing[:20]},
        )
    action_map = {"SEARCH": (1,), "INTERACTION": (2,), "READ": (3,)}
    actions = action_map.get(audience.asset.behavior_type.upper())
    if actions is None:
        raise SeedingError(
            "PLATFORM_ENUM_UNVERIFIED",
            "dynamic behavior_type is not mapped to Juguang actions",
            details={"behavior_type": audience.asset.behavior_type},
        )
    if audience.asset.lookback_days not in {3, 7, 15, 30}:
        raise SeedingError(
            "PLATFORM_ENUM_UNVERIFIED",
            "dynamic lookback_days is not a Juguang-supported period",
            details={"lookback_days": audience.asset.lookback_days},
        )
    return WireTargetInfo(
        **common,
        keywords=tuple(
            keywords_by_id[keyword_id].normalized_text
            for keyword_id in audience.asset.keyword_ids
        ),
        keyword_target_period=audience.asset.lookback_days,
        keyword_target_action=actions,
    )


def compile_feed_plan(
    *,
    project: ProjectConfig,
    plan: Mapping[str, object],
    audience: AudienceCandidate,
    keywords_by_id: Mapping[str, KeywordCandidate],
    profile: JuguangPlatformProfile,
) -> CompiledJuguangPayload:
    _require_verified_profile(profile)
    if project.advertiser.platform != "XIAOHONGSHU_JUGUANG":
        raise SeedingError(
            "PLATFORM_SCOPE_MISMATCH", "project is not a Juguang project"
        )
    campaign = _campaign(
        project=project,
        plan=plan,
        profile=profile,
        placement=profile.feed_placement,
    )
    unit = WireUnit(
        unit_name=f"UNIT-F-{_short(str(plan['identity']['logical_plan_key']), 12)}",
        target_type=profile.feed_target_type,
        target_info=_target_info_for_audience(
            audience, profile=profile, keywords_by_id=keywords_by_id
        ),
        event_bid=profile.event_bid_fen,
    )
    creative = WireCreative(
        creativity_name=f"CREATIVE-{_short(str(plan['note_id']), 16)}",
        note_id=str(plan["note_id"]),
        conversion_type=profile.creative_conversion_type,
    )
    payload = JuguangCreatePayload(
        advertiser_id=project.advertiser.advertiser_id,
        create_cascade_info_list=(
            WireCascade(
                campaign=campaign,
                unit_with_creative_list=(
                    WireUnitWithCreative(unit=unit, creativity_list=(creative,)),
                ),
            ),
        ),
    )
    return _receipt(payload, profile)


def compile_search_plan(
    *,
    project: ProjectConfig,
    plan: Mapping[str, object],
    keywords_by_id: Mapping[str, KeywordCandidate],
    keyword_decisions: Mapping[str, KeywordDecision],
    profile: JuguangPlatformProfile,
) -> CompiledJuguangPayload:
    _require_verified_profile(profile)
    keyword_ids = tuple(str(value) for value in plan["keyword_ids"])
    if not 1 <= len(keyword_ids) <= 50:
        raise SeedingError(
            "PLATFORM_KEYWORD_LIMIT_EXCEEDED",
            "Search plans require 1-50 keywords",
        )
    wire_keywords: List[WireKeywordWithBid] = []
    for keyword_id in keyword_ids:
        keyword = keywords_by_id.get(keyword_id)
        decision = keyword_decisions.get(keyword_id)
        if keyword is None or decision is None or not decision.eligible:
            raise SeedingError(
                "SOURCE_PROOF_MISSING",
                "Search keyword lacks an eligible decision receipt",
                details={"keyword_id": keyword_id},
            )
        if decision.wire_bid_fen is None:
            raise SeedingError(
                "PLATFORM_ENUM_UNVERIFIED",
                "keyword wire bid is not available",
                details={"keyword_id": keyword_id},
            )
        wire_keywords.append(
            WireKeywordWithBid(
                keyword=keyword.normalized_text,
                bid=decision.wire_bid_fen,
                phrase_match_type=profile.phrase_match_type,
            )
        )
    campaign = _campaign(
        project=project,
        plan=plan,
        profile=profile,
        placement=profile.search_placement,
    )
    unit = WireUnit(
        unit_name=f"UNIT-S-{_short(str(plan['identity']['logical_plan_key']), 12)}",
        keyword_with_bid=tuple(wire_keywords),
        target_type=profile.search_target_type,
        target_info=WireTargetInfo(
            target_gender="all",
            target_area_code=profile.target_area_code,
            target_age="all",
            target_device="all",
        ),
        event_bid=profile.event_bid_fen,
    )
    creative = WireCreative(
        creativity_name=f"CREATIVE-{_short(str(plan['note_id']), 16)}",
        note_id=str(plan["note_id"]),
        conversion_type=profile.creative_conversion_type,
    )
    payload = JuguangCreatePayload(
        advertiser_id=project.advertiser.advertiser_id,
        create_cascade_info_list=(
            WireCascade(
                campaign=campaign,
                unit_with_creative_list=(
                    WireUnitWithCreative(unit=unit, creativity_list=(creative,)),
                ),
            ),
        ),
    )
    return _receipt(payload, profile)


def _receipt(
    payload: JuguangCreatePayload, profile: JuguangPlatformProfile
) -> CompiledJuguangPayload:
    assert profile.verification_proof_sha256 is not None
    return CompiledJuguangPayload(
        payload=payload,
        payload_sha256=sha256_json(payload),
        platform_profile_id=profile.profile_id,
        platform_profile_version=profile.version,
        verification_proof_sha256=profile.verification_proof_sha256,
    )
