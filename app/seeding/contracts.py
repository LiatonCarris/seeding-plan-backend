"""Versioned, immutable contracts for the SEEDING planning branch."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Literal, Optional, Tuple, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
PROJECT_ID_PATTERN = r"^seed_[0-9a-f]{24}$"
SCHEMA_VERSION = "seeding.project.v1"
PRODUCER_VERSION = "seeding-phase0.1"


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        use_enum_values=True,
        populate_by_name=True,
    )


class StrategyMode(str, Enum):
    SEEDING = "SEEDING"


class AudienceMode(str, Enum):
    STATIC_DMP = "STATIC_DMP"
    DYNAMIC_BEHAVIOR = "DYNAMIC_BEHAVIOR"


class MetricState(str, Enum):
    READY = "READY"
    SOURCE_MATURING = "SOURCE_MATURING"
    WAITING_AUDIENCE_METRICS = "WAITING_AUDIENCE_METRICS"
    INVALID = "INVALID"


class Lane(str, Enum):
    BRAND = "BRAND"
    CATEGORY = "CATEGORY"
    SCENARIO = "SCENARIO"
    COMPETITOR = "COMPETITOR"
    GUIDE = "GUIDE"
    AUDIENCE = "AUDIENCE"
    BENEFIT = "BENEFIT"
    EVENT = "EVENT"


LANE_PRIORITY: Tuple[str, ...] = tuple(item.value for item in Lane)


class LifecycleStage(str, Enum):
    COLD_START = "COLD_START"
    VALIDATION = "VALIDATION"
    STABLE = "STABLE"
    DECLINING = "DECLINING"


class SourceWindow(FrozenModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "SourceWindow":
        if self.end_at <= self.start_at:
            raise ValueError("source window end_at must be after start_at")
        return self


class AdvertiserRef(FrozenModel):
    account_id: str = Field(min_length=1, max_length=160)
    advertiser_id: str = Field(min_length=1, max_length=160)
    v_seller_id: Optional[str] = Field(default=None, max_length=160)
    authorization_domain_id: str = Field(min_length=1, max_length=240)
    brand_id: str = Field(min_length=1, max_length=160)


class LocaleConfig(FrozenModel):
    timezone: str = Field(default="Asia/Shanghai", min_length=3, max_length=80)
    currency: Literal["CNY"] = "CNY"


class KpiConfig(FrozenModel):
    primary: Literal["CPUV", "CPE", "CPM", "CTPC", "CPI", "CPTI"]
    secondary: Tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    source: Literal["MANUAL_CONFIRMED", "YICE_CONFIRMED", "PLATFORM_CONFIRMED"]
    source_ref: str = Field(min_length=1, max_length=1000)
    confirmed_at: datetime
    confirmed_by: str = Field(min_length=1, max_length=160)

    @field_validator("secondary")
    @classmethod
    def unique_secondary(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("secondary KPI values must be unique")
        return values


class CategoryConfig(FrozenModel):
    l3_id: str = Field(min_length=1, max_length=160)
    l4_id: Optional[str] = Field(default=None, max_length=160)
    selected_level: Literal["L3", "L4"]
    source: Literal["PLATFORM", "MANUAL_CONFIRMED"]
    source_ref: str = Field(min_length=1, max_length=1000)
    taxonomy_version: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_selected_level(self) -> "CategoryConfig":
        if self.selected_level == "L4" and not self.l4_id:
            raise ValueError("l4_id is required when selected_level is L4")
        return self


class EventWindow(FrozenModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "EventWindow":
        if self.end_at <= self.start_at:
            raise ValueError("event window end_at must be after start_at")
        return self


class CampaignContext(FrozenModel):
    lifecycle_stage: LifecycleStage
    marketing_period: str = Field(min_length=1, max_length=120)
    promotion_calendar_id: Optional[str] = Field(default=None, max_length=160)
    promotion_calendar_version: Optional[str] = Field(default=None, max_length=160)
    event_window: Optional[EventWindow] = None

    @model_validator(mode="after")
    def validate_calendar_version(self) -> "CampaignContext":
        if bool(self.promotion_calendar_id) != bool(self.promotion_calendar_version):
            raise ValueError(
                "promotion_calendar_id and promotion_calendar_version must be supplied together"
            )
        return self


class ChannelConfig(FrozenModel):
    enabled: bool


class ProjectConfig(FrozenModel):
    schema_name: Literal["seeding.project.v1"] = Field(
        default=SCHEMA_VERSION, alias="schema"
    )
    strategy_mode: Literal["SEEDING"] = "SEEDING"
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    advertiser: AdvertiserRef
    locale: LocaleConfig = Field(default_factory=LocaleConfig)
    kpi: KpiConfig
    category: CategoryConfig
    campaign_context: CampaignContext
    source_windows: Dict[Literal["audience", "keyword", "note"], SourceWindow]
    feed: ChannelConfig
    search: ChannelConfig
    parameter_set_id: str = Field(min_length=1, max_length=160)
    parameter_set_version: int = Field(ge=1)
    created_by: str = Field(min_length=1, max_length=160)
    updated_by: str = Field(min_length=1, max_length=160)
    updated_at: datetime

    @model_validator(mode="after")
    def validate_sources_and_channels(self) -> "ProjectConfig":
        if set(self.source_windows) != {"audience", "keyword", "note"}:
            raise ValueError("source_windows must include audience, keyword, and note")
        if not self.feed.enabled and not self.search.enabled:
            raise ValueError("at least one channel must be enabled")
        return self


class AssetEnvelope(FrozenModel):
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    contract_version: str = Field(min_length=1, max_length=120)
    config_sha256: str = Field(pattern=SHA256_PATTERN)
    source_proof_sha256: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime
    producer_version: str = Field(
        default=PRODUCER_VERSION, min_length=1, max_length=120
    )


class StaticDmpSnapshot(AssetEnvelope):
    schema_name: Literal["seeding.static_dmp_snapshot.v1"] = Field(
        default="seeding.static_dmp_snapshot.v1", alias="schema"
    )
    package_id: str = Field(min_length=1, max_length=160)
    snapshot_at: datetime
    population: int = Field(gt=0)
    aips: int = Field(ge=0)
    i_ti: int = Field(ge=0, alias="I+TI")
    taxonomy_version: str = Field(min_length=1, max_length=160)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_metrics(self) -> "StaticDmpSnapshot":
        if not (0 <= self.i_ti <= self.aips <= self.population):
            raise ValueError("static DMP metrics must satisfy 0 <= I+TI <= AIPS <= N")
        if self.expires_at <= self.snapshot_at:
            raise ValueError("snapshot expires_at must be after snapshot_at")
        return self


class DynamicBehaviorTargeting(AssetEnvelope):
    schema_name: Literal["seeding.dynamic_behavior_targeting.v1"] = Field(
        default="seeding.dynamic_behavior_targeting.v1", alias="schema"
    )
    behavior_type: str = Field(min_length=1, max_length=120)
    keyword_ids: Tuple[str, ...] = Field(min_length=1, max_length=100)
    lookback_days: int = Field(ge=1, le=365)
    estimated_population: int = Field(gt=0)
    recomputed_at: datetime
    template_id: str = Field(min_length=1, max_length=160)
    binding_mode: Literal["SUPPLEMENT", "STANDALONE"]

    @field_validator("keyword_ids")
    @classmethod
    def unique_keyword_ids(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("keyword_ids must be unique")
        return values


AudienceAsset = Union[StaticDmpSnapshot, DynamicBehaviorTargeting]
AUDIENCE_ASSET_ADAPTER = TypeAdapter(AudienceAsset)


class AudienceCandidate(FrozenModel):
    audience_id: str = Field(min_length=1, max_length=160)
    audience_mode: AudienceMode
    source_system: str = Field(min_length=1, max_length=120)
    metric_state: MetricState
    population: int = Field(gt=0)
    hard_gates: Dict[str, bool]
    score_version: Optional[str] = Field(default=None, max_length=120)
    overlap_evidence: Dict[str, Any] = Field(default_factory=dict)
    asset: AudienceAsset

    @model_validator(mode="after")
    def validate_mode_matches_asset(self) -> "AudienceCandidate":
        expected = (
            "STATIC_DMP"
            if isinstance(self.asset, StaticDmpSnapshot)
            else "DYNAMIC_BEHAVIOR"
        )
        if self.audience_mode != expected:
            raise ValueError("audience_mode does not match the asset contract")
        asset_population = (
            self.asset.population
            if isinstance(self.asset, StaticDmpSnapshot)
            else self.asset.estimated_population
        )
        if self.population != asset_population:
            raise ValueError("candidate population does not match asset population")
        if self.metric_state == "READY" and not all(self.hard_gates.values()):
            raise ValueError("READY audience cannot contain a failed hard gate")
        return self


class KeywordCandidate(FrozenModel):
    keyword_id: str = Field(min_length=1, max_length=160)
    raw_text: str = Field(min_length=1, max_length=200)
    normalized_text: str = Field(min_length=1, max_length=200)
    primary_lane: Lane
    secondary_tags: Tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    heat: int = Field(ge=0)
    suggested_bid_fen: Optional[int] = Field(default=None, ge=0)
    source_round: Literal[1, 2]
    normalizer_version: str = Field(min_length=1, max_length=120)
    parent_keyword_id: Optional[str] = Field(default=None, max_length=160)
    source_proof_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_parent(self) -> "KeywordCandidate":
        if self.source_round == 2 and not self.parent_keyword_id:
            raise ValueError("round-2 keywords require parent_keyword_id")
        return self


class NoteCandidate(FrozenModel):
    note_id: str = Field(min_length=1, max_length=160)
    url: str = Field(pattern=r"^https://", max_length=1000)
    status: Literal["AVAILABLE", "UNAVAILABLE", "REMOVED"]
    content_facts: Tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    eligibility: bool
    proof: Dict[str, Any]
    snapshot_sha256: str = Field(pattern=SHA256_PATTERN)


class PairCandidate(FrozenModel):
    note_id: str = Field(min_length=1, max_length=160)
    asset_id: str = Field(min_length=1, max_length=160)
    pair_type: Literal["NOTE_AUDIENCE", "NOTE_KEYWORD_LANE"]
    pair_score: float = Field(ge=0, le=100)
    match_grade: Literal["A", "B", "C", "REVIEW"]
    evidence_spans: Tuple[str, ...] = Field(min_length=1, max_length=40)
    reason_codes: Tuple[str, ...] = Field(default_factory=tuple, max_length=40)
    model_id: str = Field(min_length=1, max_length=160)
    prompt_version: str = Field(min_length=1, max_length=120)
    rule_version: str = Field(min_length=1, max_length=120)
    human_override: Optional[Dict[str, Any]] = None


class PlanIdentity(FrozenModel):
    logical_plan_key: str = Field(pattern=SHA256_PATTERN)
    plan_revision_id: str = Field(min_length=1, max_length=160)
    supersedes_plan_revision_id: Optional[str] = Field(default=None, max_length=160)
    stage: str = Field(min_length=1, max_length=80)
    revision_status: Literal["DRAFT", "ACTIVE", "SUPERSEDED"]


class FeedPlanCandidate(FrozenModel):
    note_id: str
    audience_id: str
    objective: str
    daily_budget_fen: int = Field(gt=0)
    expected_daily_spend_fen: int = Field(gt=0)
    identity: PlanIdentity


class SearchPlanCandidate(FrozenModel):
    note_id: str
    primary_lane: Lane
    keyword_ids: Tuple[str, ...] = Field(min_length=1, max_length=50)
    bid_mode: Literal["OCPX_STABLE_COST", "MANUAL_KEYWORD"]
    release_evidence: Dict[str, Any]
    identity: PlanIdentity

    @field_validator("keyword_ids")
    @classmethod
    def unique_keywords(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Search plan keyword_ids must be unique")
        return values


class ExecutionGrant(FrozenModel):
    grant_id: str
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    environment: Literal["TEST"]
    account_id: str
    authorization_domain_id: str
    allowed_actions: Tuple[Literal["CREATE", "PAUSE", "READBACK"], ...]
    object_hashes: Tuple[str, ...]
    config_sha: str = Field(pattern=SHA256_PATTERN)
    matrix_sha: str = Field(pattern=SHA256_PATTERN)
    max_attempts: int = Field(ge=1, le=10)
    expires_at: datetime
    nonce: str
    approver: str
    consumed_at: Optional[datetime] = None


class ReleaseGrant(FrozenModel):
    release_grant_id: str
    environment: Literal["PRODUCTION"]
    plan_revision_ids: Tuple[str, ...] = Field(min_length=1)
    account_id: str
    authorization_domain_id: str
    allowed_actions: Tuple[Literal["ENABLE", "PAUSE"], ...]
    start_at: datetime
    spend_cap_fen: int = Field(gt=0)
    spend_cap_period: Literal["TOTAL", "DAILY"]
    expires_at: datetime
    approver: str

    @model_validator(mode="after")
    def validate_dates(self) -> "ReleaseGrant":
        if self.expires_at <= self.start_at:
            raise ValueError("ReleaseGrant expires_at must be after start_at")
        return self


class PrepareBudget(FrozenModel):
    platform_daily_budget_fen: int = Field(gt=0)
    expected_daily_spend_fen: int = Field(gt=0)
    note_total_cap_fen: int = Field(gt=0)
    phase_budget_fen: int = Field(gt=0)
    exploration_cap_fen: int = Field(ge=0)
    observation_days: int = Field(ge=1, le=30)
    business_audience_cap: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def validate_caps(self) -> "PrepareBudget":
        if self.exploration_cap_fen > self.phase_budget_fen:
            raise ValueError("exploration_cap_fen cannot exceed phase_budget_fen")
        return self


class PrepareRequest(FrozenModel):
    config_sha: str = Field(pattern=SHA256_PATTERN)
    stage: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=120)
    notes: Tuple[NoteCandidate, ...] = Field(min_length=1, max_length=500)
    audiences: Tuple[AudienceCandidate, ...] = Field(min_length=1, max_length=5000)
    keywords: Tuple[KeywordCandidate, ...] = Field(
        default_factory=tuple, max_length=5000
    )
    pairs: Tuple[PairCandidate, ...] = Field(min_length=1, max_length=200000)
    budget: PrepareBudget
    min_pair_score: float = Field(ge=0, le=100)
    parameter_review_confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_unique_assets(self) -> "PrepareRequest":
        collections = (
            ("note_id", [item.note_id for item in self.notes]),
            ("audience_id", [item.audience_id for item in self.audiences]),
            ("keyword_id", [item.keyword_id for item in self.keywords]),
        )
        for label, values in collections:
            if len(values) != len(set(values)):
                raise ValueError(f"{label} values must be unique")
        return self


class ConfirmRequest(FrozenModel):
    config_sha: str = Field(pattern=SHA256_PATTERN)
    matrix_sha: str = Field(pattern=SHA256_PATTERN)
    object_hashes: Tuple[str, ...] = Field(min_length=1)
    confirmed_by: str = Field(min_length=1, max_length=160)
    confirmed_at: datetime
