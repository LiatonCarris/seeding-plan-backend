from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from seeding_fixtures import NOW, SHA, audience, project_config

from app.seeding.contracts import (
    AudienceCandidate,
    DynamicBehaviorTargeting,
    SearchPlanCandidate,
    StaticDmpSnapshot,
)
from app.seeding.identity import logical_plan_key


def test_project_contract_contains_explicit_account_category_and_campaign_identity() -> (
    None
):
    config = project_config()
    assert config.advertiser.advertiser_id == 12345
    assert config.advertiser.environment == "TEST"
    assert config.advertiser.account_evidence_sha256 == SHA
    assert config.advertiser.authorization_domain_id == "domain-1"
    assert config.category.selected_level == "L4"
    assert config.campaign_context.lifecycle_stage == "COLD_START"
    assert not hasattr(config, "formal_execution_enabled")


def test_static_snapshot_enforces_t_a_n_order() -> None:
    with pytest.raises(ValidationError):
        StaticDmpSnapshot(
            project_id="seed_" + "1" * 24,
            contract_version="v1",
            config_sha256=SHA,
            source_proof_sha256=SHA,
            created_at=NOW,
            package_id="pkg",
            package_name="package",
            group_id="group",
            snapshot_at=NOW,
            population=100,
            aips=20,
            i_ti=21,
            taxonomy_version="v1",
            expires_at=NOW + timedelta(days=1),
        )


def test_dynamic_behavior_cannot_be_silently_labeled_static() -> None:
    asset = DynamicBehaviorTargeting(
        project_id="seed_" + "1" * 24,
        contract_version="v1",
        config_sha256=SHA,
        source_proof_sha256=SHA,
        created_at=NOW,
        behavior_type="SEARCH",
        keyword_ids=("k1",),
        lookback_days=30,
        estimated_population=1000,
        recomputed_at=NOW,
        template_id="template-1",
        binding_mode="SUPPLEMENT",
    )
    with pytest.raises(ValidationError):
        AudienceCandidate(
            audience_id="aud-dynamic",
            audience_mode="STATIC_DMP",
            source_system="LINGXI",
            metric_state="READY",
            population=1000,
            hard_gates={"availability": True},
            asset=asset,
        )


def test_logical_plan_key_does_not_include_config_sha_or_grant() -> None:
    kwargs = dict(
        account_id="account-1",
        project_id="seed_" + "1" * 24,
        stage="COLD_START",
        channel="FEED",
        note_id="note-1",
        objective="NOTE_ENGAGEMENT",
        audience_id="aud-1",
    )
    assert logical_plan_key(**kwargs) == logical_plan_key(**kwargs)


def test_search_plan_has_business_limit_of_fifty_keywords() -> None:
    from app.seeding.contracts import PlanIdentity

    identity = PlanIdentity(
        logical_plan_key=SHA,
        plan_revision_id="planrev-1",
        stage="SEARCH",
        revision_status="ACTIVE",
    )
    with pytest.raises(ValidationError):
        SearchPlanCandidate(
            note_id="note-1",
            primary_lane="BRAND",
            keyword_ids=tuple(f"k-{index}" for index in range(51)),
            bid_mode="OCPX_STABLE_COST",
            daily_budget_fen=10_000,
            expected_daily_spend_fen=5_000,
            release_evidence={"eligible": True},
            identity=identity,
        )


def test_static_fixture_is_ready_and_consistent() -> None:
    candidate = audience(1)
    assert candidate.audience_mode == "STATIC_DMP"
    assert candidate.asset.i_ti <= candidate.asset.aips <= candidate.asset.population
