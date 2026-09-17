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
PRODUCER_VERSION = "seeding-0.2.0"


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


class ParameterStatus(str, Enum):
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FROZEN = "FROZEN"


class OverlapPolicy(str, Enum):
    WARNING_ONLY = "WARNING_ONLY"
    DIRECTIONAL_CONTAINMENT = "DIRECTIONAL_CONTAINMENT"
    JACCARD = "JACCARD"


class SourceWindow(FrozenModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "SourceWindow":
        if self.end_at <= self.start_at:
            raise ValueError("source window end_at must be after start_at")
        return self


class AdvertiserRef(FrozenModel):
    platform: Literal["XIAOHONGSHU_JUGUANG"] = "XIAOHONGSHU_JUGUANG"
    environment: Literal["TEST", "PRODUCTION"]
    account_id: str = Field(min_length=1, max_length=160)
    advertiser_id: int = Field(gt=0)
    advertiser_account_name: str = Field(min_length=1, max_length=240)
    v_seller_id: Optional[str] = Field(default=None, max_length=160)
    authorization_domain_id: str = Field(min_length=1, max_length=240)
    brand_id: str = Field(min_length=1, max_length=160)
    store_id: Optional[str] = Field(default=None, max_length=160)
    account_owner_principal_id: str = Field(min_length=1, max_length=160)
    account_evidence_sha256: str = Field(pattern=SHA256_PATTERN)
    account_verified_at: datetime


class LocaleConfig(FrozenModel):
    timezone: str = Field(default="Asia/Shanghai", min_length=3, max_length=80)
    currency: Literal["CNY"] = "CNY"


class KpiConfig(FrozenModel):
    primary: Literal["CPUV", "CPE", "CPM", "CTPC", "CPI", "CPTI"]
    secondary: Tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    source: Literal["MANUAL_CONFIRMED", "YICE_CONFIRMED", "PLATFORM_CONFIRMED"]
    source_ref: str = Field(min_length=1, max_length=1000)
    source_proof_sha256: str = Field(pattern=SHA256_PATTERN)
    confirmed_at: datetime
    confirmed_by: str = Field(min_length=1, max_length=160)

    @field_validator("secondary")
    @classmethod
    def unique_secondary(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("secondary KPI values must be unique")
        return values


class CategoryConfig(FrozenModel):
    business_category_id: str = Field(min_length=1, max_length=160)
    l3_id: str = Field(min_length=1, max_length=160)
    l4_id: Optional[str] = Field(default=None, max_length=160)
    category_name: str = Field(min_length=1, max_length=240)
    selected_level: Literal["L3", "L4"]
    source: Literal["PLATFORM", "MANUAL_CONFIRMED"]
    source_ref: str = Field(min_length=1, max_length=1000)
    source_system: str = Field(min_length=1, max_length=120)
    source_snapshot_at: datetime
    source_proof_sha256: str = Field(pattern=SHA256_PATTERN)
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


class ProjectMemberRequest(FrozenModel):
    principal_id: str = Field(min_length=1, max_length=160)
    access_level: Literal["READ", "WRITE", "APPROVE", "EXECUTE", "ADMIN"]


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
    package_name: str = Field(min_length=1, max_length=240)
    group_id: str = Field(min_length=1, max_length=160)
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
    daily_budget_fen: int = Field(gt=0)
    expected_daily_spend_fen: int = Field(gt=0)
    release_evidence: Dict[str, Any]
    identity: PlanIdentity

    @field_validator("keyword_ids")
    @classmethod
    def unique_keywords(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Search plan keyword_ids must be unique")
        return values


class AudienceOverlapEvidence(FrozenModel):
    left_audience_id: str = Field(min_length=1, max_length=160)
    right_audience_id: str = Field(min_length=1, max_length=160)
    intersection: int = Field(ge=0)
    source_proof_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_distinct_audiences(self) -> "AudienceOverlapEvidence":
        if self.left_audience_id == self.right_audience_id:
            raise ValueError("overlap evidence requires two different audiences")
        return self


class SemanticDuplicateEvidence(FrozenModel):
    left_keyword_id: str = Field(min_length=1, max_length=160)
    right_keyword_id: str = Field(min_length=1, max_length=160)
    similarity: float = Field(ge=0, le=1)
    semantic_model_id: str = Field(min_length=1, max_length=240)
    prompt_version: str = Field(min_length=1, max_length=120)
    embedding_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_distinct_keywords(self) -> "SemanticDuplicateEvidence":
        if self.left_keyword_id == self.right_keyword_id:
            raise ValueError("semantic evidence requires two different keywords")
        return self


class SearchReleaseEvidence(FrozenModel):
    note_id: str = Field(min_length=1, max_length=160)
    observation_days: int = Field(ge=0)
    spend_fen: int = Field(ge=0)
    impressions: int = Field(ge=0)
    primary_kpi_value: float = Field(ge=0)
    data_quality_ok: bool
    note_available: bool
    source_proof_sha256: str = Field(pattern=SHA256_PATTERN)


class SearchReleaseGate(FrozenModel):
    min_observation_days: int = Field(ge=1, le=30)
    min_spend_fen: int = Field(ge=0)
    min_impressions: int = Field(ge=0)
    min_primary_kpi_value: float = Field(ge=0)
    primary_kpi_comparison: Literal["GTE", "LTE"] = "GTE"


class JuguangPlatformProfile(FrozenModel):
    profile_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    verified: bool
    verification_proof_sha256: Optional[str] = Field(
        default=None, pattern=SHA256_PATTERN
    )
    verified_at: Optional[datetime] = None
    marketing_target: Literal[4] = 4
    promotion_target: Literal[1] = 1
    feed_placement: Literal[1] = 1
    search_placement: Literal[2] = 2
    optimize_objective: int = Field(ge=0)
    deep_optimize_objective: int = Field(default=-1, ge=-1)
    bidding_strategy: Literal[2, 3, 7]
    delivery_mode: Literal[0, 1] = 0
    feed_target_type: Literal[2, 3]
    search_target_type: Literal[0] = 0
    phrase_match_type: Literal[0, 1] = 1
    event_bid_fen: int = Field(ge=0)
    minimum_campaign_budget_fen: int = Field(gt=0)
    maximum_campaign_budget_fen: int = Field(gt=0)
    target_area_code: str = Field(min_length=1, max_length=2000)
    target_gender: Literal["all", "0", "1"] = "all"
    target_age: str = Field(default="all", min_length=1, max_length=200)
    target_device: Literal["all", "ios", "android"] = "all"
    creative_conversion_type: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_verified_profile(self) -> "JuguangPlatformProfile":
        if self.minimum_campaign_budget_fen > self.maximum_campaign_budget_fen:
            raise ValueError("platform budget minimum must not exceed maximum")
        if self.verified and (
            not self.verification_proof_sha256 or self.verified_at is None
        ):
            raise ValueError("verified platform profile requires proof and timestamp")
        return self


class SeedingParameterSet(FrozenModel):
    parameter_set_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    status: ParameterStatus
    penetration_metric: Optional[Literal["PenAIPS", "PenTI"]] = None
    penetration_weight: float = Field(default=0.7, ge=0, le=1)
    deep_weight: float = Field(default=0.3, ge=0, le=1)
    audience_min_population: Optional[int] = Field(default=None, gt=0)
    audience_max_population: int = Field(default=20_000_000, gt=0)
    overlap_policy: OverlapPolicy = OverlapPolicy.WARNING_ONLY
    overlap_threshold: Optional[float] = Field(default=None, gt=0, le=1)
    semantic_dedupe_enabled: bool = False
    semantic_similarity_threshold: Optional[float] = Field(default=None, gt=0, le=1)
    semantic_model_id: Optional[str] = Field(default=None, max_length=240)
    alias_dictionary_version: str = Field(min_length=1, max_length=120)
    keyword_category_heat: int = Field(ge=0)
    keyword_comparable_category_heats: Tuple[int, ...] = Field(default_factory=tuple)
    keyword_business_limit: int = Field(default=50, ge=1, le=50)
    platform_keyword_limit: int = Field(default=100, ge=50, le=100)
    min_bid_fen: int = Field(default=0, ge=0)
    max_bid_fen: int = Field(default=100_000, ge=0)
    bid_step_fen: int = Field(default=1, gt=0)
    search_release_gate: Optional[SearchReleaseGate] = None
    juguang_profile: Optional[JuguangPlatformProfile] = None

    @model_validator(mode="after")
    def validate_frozen_parameters(self) -> "SeedingParameterSet":
        if not abs(self.penetration_weight + self.deep_weight - 1.0) <= 1e-9:
            raise ValueError("audience score weights must sum to 1")
        if self.audience_max_population < (self.audience_min_population or 1):
            raise ValueError("audience_max_population must not be below the minimum")
        if self.min_bid_fen > self.max_bid_fen:
            raise ValueError("min_bid_fen must not exceed max_bid_fen")
        if self.status == "FROZEN":
            if self.penetration_metric is None:
                raise ValueError("frozen parameters require penetration_metric")
            if self.audience_min_population is None:
                raise ValueError("frozen parameters require audience_min_population")
            if self.overlap_policy != "WARNING_ONLY" and self.overlap_threshold is None:
                raise ValueError("a hard overlap policy requires a threshold")
            if self.semantic_dedupe_enabled and (
                self.semantic_similarity_threshold is None or not self.semantic_model_id
            ):
                raise ValueError(
                    "enabled semantic dedupe requires a threshold and semantic_model_id"
                )
        return self


class ExecutionGrant(FrozenModel):
    grant_id: str
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    environment: Literal["TEST"]
    platform: Literal["XIAOHONGSHU_JUGUANG"] = "XIAOHONGSHU_JUGUANG"
    advertiser_id: int = Field(gt=0)
    account_id: str
    authorization_domain_id: str
    allowed_actions: Tuple[
        Literal["CREATE", "PAUSE", "READBACK", "RELOCK", "RECONCILE"], ...
    ]
    plan_revision_ids: Tuple[str, ...] = Field(min_length=1)
    object_hashes: Tuple[str, ...]
    platform_payload_hashes: Tuple[str, ...] = Field(min_length=1)
    config_sha: str = Field(pattern=SHA256_PATTERN)
    matrix_sha: str = Field(pattern=SHA256_PATTERN)
    confirmation_sha: str = Field(pattern=SHA256_PATTERN)
    max_attempts: int = Field(ge=1, le=10)
    issued_at: datetime
    expires_at: datetime
    nonce: str
    approver_id: str
    approver_role: Literal["approver", "admin"]
    grant_sha256: str = Field(pattern=SHA256_PATTERN)
    signature_sha256: str = Field(pattern=SHA256_PATTERN)
    consumed_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_execution_grant(self) -> "ExecutionGrant":
        if self.expires_at <= self.issued_at:
            raise ValueError("ExecutionGrant expires_at must be after issued_at")
        if len(self.plan_revision_ids) != len(set(self.plan_revision_ids)):
            raise ValueError("ExecutionGrant plan_revision_ids must be unique")
        if len(self.plan_revision_ids) != len(self.platform_payload_hashes):
            raise ValueError("each plan revision requires one platform payload hash")
        return self


class IssueExecutionGrantRequest(FrozenModel):
    config_sha: str = Field(pattern=SHA256_PATTERN)
    matrix_sha: str = Field(pattern=SHA256_PATTERN)
    confirmation_sha: str = Field(pattern=SHA256_PATTERN)
    object_hashes: Tuple[str, ...] = Field(min_length=1)
    plan_revision_ids: Tuple[str, ...] = Field(min_length=1)
    platform_payload_hashes: Tuple[str, ...] = Field(min_length=1)
    max_attempts: int = Field(default=1, ge=1, le=3)
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=240)


class ExecuteRequest(FrozenModel):
    grant_id: str = Field(min_length=1, max_length=160)
    grant_sha256: str = Field(pattern=SHA256_PATTERN)
    signature_sha256: str = Field(pattern=SHA256_PATTERN)


class RevokeGrantRequest(FrozenModel):
    reason: str = Field(min_length=3, max_length=1000)


class PausedCreateGrant(FrozenModel):
    grant_id: str
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    environment: Literal["PRODUCTION"]
    platform: Literal["XIAOHONGSHU_JUGUANG"] = "XIAOHONGSHU_JUGUANG"
    advertiser_id: int = Field(gt=0)
    account_id: str
    authorization_domain_id: str
    allowed_actions: Tuple[
        Literal["CREATE_PAUSED", "PAUSE", "READBACK", "RELOCK", "RECONCILE"], ...
    ]
    plan_revision_ids: Tuple[str, ...] = Field(min_length=1)
    object_hashes: Tuple[str, ...] = Field(min_length=1)
    platform_payload_hashes: Tuple[str, ...] = Field(min_length=1)
    config_sha: str = Field(pattern=SHA256_PATTERN)
    matrix_sha: str = Field(pattern=SHA256_PATTERN)
    confirmation_sha: str = Field(pattern=SHA256_PATTERN)
    test_readback_receipt_sha: str = Field(pattern=SHA256_PATTERN)
    issued_at: datetime
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=240)
    approver_id: str
    approver_role: Literal["approver", "admin"]
    grant_sha256: str = Field(pattern=SHA256_PATTERN)
    signature_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_paused_create_grant(self) -> "PausedCreateGrant":
        if self.expires_at <= self.issued_at:
            raise ValueError("PausedCreateGrant expires_at must be after issued_at")
        if len(self.plan_revision_ids) != len(self.platform_payload_hashes):
            raise ValueError("each plan revision requires one platform payload hash")
        return self


class IssuePausedCreateGrantRequest(FrozenModel):
    config_sha: str = Field(pattern=SHA256_PATTERN)
    matrix_sha: str = Field(pattern=SHA256_PATTERN)
    confirmation_sha: str = Field(pattern=SHA256_PATTERN)
    test_readback_receipt_sha: str = Field(pattern=SHA256_PATTERN)
    object_hashes: Tuple[str, ...] = Field(min_length=1)
    plan_revision_ids: Tuple[str, ...] = Field(min_length=1)
    platform_payload_hashes: Tuple[str, ...] = Field(min_length=1)
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=240)


class ReleaseObjectBinding(FrozenModel):
    plan_revision_id: str = Field(min_length=1, max_length=160)
    campaign_id: int = Field(gt=0)
    unit_id: int = Field(gt=0)
    creativity_ids: Tuple[int, ...] = Field(min_length=1)

    @field_validator("creativity_ids")
    @classmethod
    def valid_creativity_ids(cls, values: Tuple[int, ...]) -> Tuple[int, ...]:
        if any(value <= 0 for value in values) or len(values) != len(set(values)):
            raise ValueError("creativity_ids must be unique positive values")
        return values


class ReleaseGrant(FrozenModel):
    release_grant_id: str
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    environment: Literal["PRODUCTION"]
    platform: Literal["XIAOHONGSHU_JUGUANG"] = "XIAOHONGSHU_JUGUANG"
    advertiser_id: int = Field(gt=0)
    plan_revision_ids: Tuple[str, ...] = Field(min_length=1)
    platform_campaign_ids: Tuple[int, ...] = Field(min_length=1)
    platform_object_bindings: Tuple[ReleaseObjectBinding, ...] = Field(min_length=1)
    account_id: str
    authorization_domain_id: str
    allowed_actions: Tuple[Literal["ENABLE", "PAUSE"], ...]
    config_sha: str = Field(pattern=SHA256_PATTERN)
    matrix_sha: str = Field(pattern=SHA256_PATTERN)
    confirmation_sha: str = Field(pattern=SHA256_PATTERN)
    payload_sha: str = Field(pattern=SHA256_PATTERN)
    readback_receipt_sha: str = Field(pattern=SHA256_PATTERN)
    start_at: datetime
    spend_cap_fen: int = Field(gt=0)
    spend_cap_period: Literal["TOTAL", "DAILY"]
    spend_monitor_source: str = Field(min_length=1, max_length=240)
    spend_monitor_proof_sha256: str = Field(pattern=SHA256_PATTERN)
    exceed_action: Literal["PAUSE_AND_RELOCK"] = "PAUSE_AND_RELOCK"
    issued_at: datetime
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=240)
    approver_id: str
    approver_role: Literal["approver", "admin"]
    grant_sha256: str = Field(pattern=SHA256_PATTERN)
    signature_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_dates(self) -> "ReleaseGrant":
        if self.expires_at <= self.start_at:
            raise ValueError("ReleaseGrant expires_at must be after start_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("ReleaseGrant must not already be expired when issued")
        if len(self.plan_revision_ids) != len(self.platform_campaign_ids):
            raise ValueError("each released plan requires one exact campaign id")
        if len(self.plan_revision_ids) != len(self.platform_object_bindings):
            raise ValueError("each released plan requires one exact object binding")
        if (
            tuple(item.plan_revision_id for item in self.platform_object_bindings)
            != self.plan_revision_ids
        ):
            raise ValueError("object bindings must follow plan_revision_ids order")
        if (
            tuple(item.campaign_id for item in self.platform_object_bindings)
            != self.platform_campaign_ids
        ):
            raise ValueError("campaign ids must match object bindings")
        if len(self.plan_revision_ids) != len(set(self.plan_revision_ids)):
            raise ValueError("release plan revisions must be unique")
        return self


class IssueReleaseGrantRequest(FrozenModel):
    plan_revision_ids: Tuple[str, ...] = Field(min_length=1)
    platform_campaign_ids: Tuple[int, ...] = Field(min_length=1)
    platform_object_bindings: Tuple[ReleaseObjectBinding, ...] = Field(min_length=1)
    config_sha: str = Field(pattern=SHA256_PATTERN)
    matrix_sha: str = Field(pattern=SHA256_PATTERN)
    confirmation_sha: str = Field(pattern=SHA256_PATTERN)
    payload_sha: str = Field(pattern=SHA256_PATTERN)
    readback_receipt_sha: str = Field(pattern=SHA256_PATTERN)
    start_at: datetime
    spend_cap_fen: int = Field(gt=0)
    spend_cap_period: Literal["TOTAL", "DAILY"]
    spend_monitor_source: str = Field(min_length=1, max_length=240)
    spend_monitor_proof_sha256: str = Field(pattern=SHA256_PATTERN)
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=240)


class ReleaseExecuteRequest(FrozenModel):
    grant_sha256: str = Field(pattern=SHA256_PATTERN)
    signature_sha256: str = Field(pattern=SHA256_PATTERN)


class ReleaseEvaluationRequest(FrozenModel):
    evaluation_key: str = Field(min_length=8, max_length=160)
    grant_sha256: str = Field(pattern=SHA256_PATTERN)
    signature_sha256: str = Field(pattern=SHA256_PATTERN)


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


class JuguangKeywordSeed(FrozenModel):
    primary_lane: Lane
    request_type: Literal["note", "industry", "search", "session"]
    keyword: Optional[str] = Field(default=None, max_length=200)
    item_ids: Tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    taxonomy_id: Optional[str] = Field(default=None, max_length=160)
    promotion_target: Optional[int] = Field(default=None, ge=0)
    rank: Optional[Literal[1, 2, 3, 4]] = None
    source_round: Literal[1, 2] = 1
    parent_keyword_id: Optional[str] = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_seed(self) -> "JuguangKeywordSeed":
        if self.request_type == "note" and (
            self.promotion_target is None or not self.item_ids
        ):
            raise ValueError("note keyword seed requires promotion_target and item_ids")
        if self.request_type == "industry" and not self.taxonomy_id:
            raise ValueError("industry keyword seed requires taxonomy_id")
        if self.request_type in {"search", "session"} and not self.keyword:
            raise ValueError("search/session keyword seed requires keyword")
        if self.source_round == 2 and not self.parent_keyword_id:
            raise ValueError("round-2 keyword seed requires parent_keyword_id")
        return self


class JuguangSourceSyncRequest(FrozenModel):
    marketing_target: Literal[4] = 4
    audience_estimate_placement: Literal[1, 2, 4, 7] = 1
    audience_estimate_optimize_target: int = Field(ge=0)
    audience_package_values: Tuple[str, ...] = Field(
        default_factory=tuple, max_length=200
    )
    keyword_seeds: Tuple[JuguangKeywordSeed, ...] = Field(
        default_factory=tuple, max_length=200
    )
    include_word_bags: bool = True
    word_bag_max_pages: int = Field(default=1, ge=1, le=20)

    @field_validator("audience_package_values")
    @classmethod
    def unique_packages(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("audience_package_values must be unique")
        return values


class LingxiAudienceMetric(FrozenModel):
    package_value: str = Field(min_length=1, max_length=160)
    package_name: str = Field(min_length=1, max_length=240)
    group_id: str = Field(min_length=1, max_length=160)
    population: int = Field(gt=0)
    aips: int = Field(ge=0)
    i_ti: int = Field(ge=0, alias="I+TI")
    snapshot_at: datetime
    expires_at: datetime
    taxonomy_version: str = Field(min_length=1, max_length=160)
    source_proof_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_metrics(self) -> "LingxiAudienceMetric":
        if not (0 <= self.i_ti <= self.aips <= self.population):
            raise ValueError("Lingxi metrics must satisfy 0 <= I+TI <= AIPS <= N")
        if self.expires_at <= self.snapshot_at:
            raise ValueError("Lingxi metric expiry must follow snapshot time")
        return self


class LingxiAudienceImportRequest(FrozenModel):
    source_ref: str = Field(min_length=1, max_length=1000)
    rows: Tuple[LingxiAudienceMetric, ...] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def unique_rows(self) -> "LingxiAudienceImportRequest":
        values = tuple(item.package_value for item in self.rows)
        if len(values) != len(set(values)):
            raise ValueError("Lingxi package values must be unique")
        return self


class PrepareRequest(FrozenModel):
    config_sha: str = Field(pattern=SHA256_PATTERN)
    stage: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=120)
    bid_mode: Literal["OCPX_STABLE_COST", "MANUAL_KEYWORD"] = "OCPX_STABLE_COST"
    notes: Tuple[NoteCandidate, ...] = Field(min_length=1, max_length=500)
    audiences: Tuple[AudienceCandidate, ...] = Field(min_length=1, max_length=5000)
    keywords: Tuple[KeywordCandidate, ...] = Field(
        default_factory=tuple, max_length=5000
    )
    pairs: Tuple[PairCandidate, ...] = Field(min_length=1, max_length=200000)
    parameter_set: SeedingParameterSet
    overlap_evidence: Tuple[AudienceOverlapEvidence, ...] = Field(
        default_factory=tuple, max_length=200000
    )
    semantic_duplicate_evidence: Tuple[SemanticDuplicateEvidence, ...] = Field(
        default_factory=tuple, max_length=200000
    )
    search_release_evidence: Tuple[SearchReleaseEvidence, ...] = Field(
        default_factory=tuple, max_length=500
    )
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
        if self.parameter_set.parameter_set_id == "":
            raise ValueError("parameter_set_id is required")
        return self


class ConfirmRequest(FrozenModel):
    config_sha: str = Field(pattern=SHA256_PATTERN)
    matrix_sha: str = Field(pattern=SHA256_PATTERN)
    object_hashes: Tuple[str, ...] = Field(min_length=1)
    confirmed_by: str = Field(min_length=1, max_length=160)
    confirmed_at: datetime
