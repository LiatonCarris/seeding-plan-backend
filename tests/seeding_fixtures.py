from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.seeding.contracts import (
    AdvertiserRef,
    AudienceCandidate,
    CampaignContext,
    CategoryConfig,
    ChannelConfig,
    KpiConfig,
    LocaleConfig,
    MetricState,
    NoteCandidate,
    PairCandidate,
    PrepareBudget,
    PrepareRequest,
    ProjectConfig,
    SourceWindow,
    StaticDmpSnapshot,
)
from app.seeding.identity import sha256_json

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def project_config(project_id: str = "seed_" + "1" * 24) -> ProjectConfig:
    window = SourceWindow(start_at=NOW - timedelta(days=30), end_at=NOW)
    return ProjectConfig(
        schema="seeding.project.v1",
        project_id=project_id,
        advertiser=AdvertiserRef(
            account_id="account-1",
            advertiser_id="advertiser-1",
            v_seller_id="seller-1",
            authorization_domain_id="domain-1",
            brand_id="brand-1",
        ),
        locale=LocaleConfig(),
        kpi=KpiConfig(
            primary="CPUV",
            source="MANUAL_CONFIRMED",
            source_ref="decision-1",
            confirmed_at=NOW,
            confirmed_by="owner-1",
        ),
        category=CategoryConfig(
            l3_id="l3",
            l4_id="l4",
            selected_level="L4",
            source="MANUAL_CONFIRMED",
            source_ref="category-proof-1",
            taxonomy_version="2026-09",
        ),
        campaign_context=CampaignContext(
            lifecycle_stage="COLD_START",
            marketing_period="ALWAYS_ON",
        ),
        source_windows={"audience": window, "keyword": window, "note": window},
        feed=ChannelConfig(enabled=True),
        search=ChannelConfig(enabled=False),
        parameter_set_id="review-confirmed-test-v1",
        parameter_set_version=1,
        created_by="owner-1",
        updated_by="owner-1",
        updated_at=NOW,
    )


def audience(index: int) -> AudienceCandidate:
    population = 3_000_000 + index * 100_000
    snapshot = StaticDmpSnapshot(
        project_id="seed_" + "1" * 24,
        contract_version="seeding.static_dmp_snapshot.v1",
        config_sha256="b" * 64,
        source_proof_sha256=SHA,
        created_at=NOW,
        package_id=f"pkg-{index}",
        snapshot_at=NOW - timedelta(hours=1),
        population=population,
        aips=300_000 + index * 10_000,
        i_ti=30_000 + index * 1_000,
        taxonomy_version="2026-09",
        expires_at=NOW + timedelta(days=1),
    )
    return AudienceCandidate(
        audience_id=f"aud-{index}",
        audience_mode="STATIC_DMP",
        source_system="LINGXI",
        metric_state=MetricState.READY,
        population=population,
        hard_gates={
            "availability": True,
            "size": True,
            "quality": True,
            "policy": True,
            "relevance": True,
        },
        score_version="score-v1",
        asset=snapshot,
    )


def note(index: int) -> NoteCandidate:
    return NoteCandidate(
        note_id=f"note-{index}",
        url=f"https://www.xiaohongshu.com/explore/note-{index}",
        status="AVAILABLE",
        content_facts=("fact",),
        eligibility=True,
        proof={"source": "confirmed"},
        snapshot_sha256=SHA,
    )


def pair(note_index: int, audience_index: int, score: float = 90) -> PairCandidate:
    return PairCandidate(
        note_id=f"note-{note_index}",
        asset_id=f"aud-{audience_index}",
        pair_type="NOTE_AUDIENCE",
        pair_score=score,
        match_grade="A",
        evidence_spans=("confirmed product benefit",),
        model_id="model-1",
        prompt_version="prompt-1",
        rule_version="rule-1",
    )


def prepare_request(
    config: ProjectConfig,
    *,
    phase_budget_fen: int = 120_000,
    pair_score: float = 90,
) -> PrepareRequest:
    audiences = tuple(audience(index) for index in range(1, 4))
    notes = tuple(note(index) for index in range(1, 3))
    pairs = tuple(
        pair(note_index, audience_index, pair_score)
        for note_index in range(1, 3)
        for audience_index in range(1, 4)
    )
    return PrepareRequest(
        config_sha=sha256_json(config),
        stage="COLD_START",
        objective="NOTE_ENGAGEMENT",
        notes=notes,
        audiences=audiences,
        pairs=pairs,
        budget=PrepareBudget(
            platform_daily_budget_fen=10_000,
            expected_daily_spend_fen=10_000,
            note_total_cap_fen=50_000,
            phase_budget_fen=phase_budget_fen,
            exploration_cap_fen=20_000,
            observation_days=3,
            business_audience_cap=3,
        ),
        min_pair_score=80,
        parameter_review_confirmed=True,
    )
