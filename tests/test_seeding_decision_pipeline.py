from __future__ import annotations

import pytest

from seeding_fixtures import NOW, SHA, audience, prepare_request, project_config

from app.seeding.contracts import (
    AudienceCandidate,
    AudienceOverlapEvidence,
    DynamicBehaviorTargeting,
    KeywordCandidate,
    PairCandidate,
    PrepareRequest,
    ProjectConfig,
    SearchReleaseEvidence,
    SearchReleaseGate,
    SemanticDuplicateEvidence,
)
from app.seeding.decision import (
    evaluate_audiences,
    evaluate_keywords,
    search_release_allowed,
)
from app.seeding.errors import SeedingError
from app.seeding.identity import sha256_json
from app.seeding.service import SeedingService
from app.seeding.store import SeedingStore


def keywords(count: int = 5) -> tuple[KeywordCandidate, ...]:
    return tuple(
        KeywordCandidate(
            keyword_id=f"kw-{index}",
            raw_text=f"防晒霜 {index}",
            normalized_text=f"防晒霜 {index}",
            primary_lane="BRAND",
            heat=8_000 + index * 100,
            suggested_bid_fen=100 + index * 10,
            source_round=1,
            normalizer_version="nfkc-safe-punctuation-v1",
            source_proof_sha256=SHA,
        )
        for index in range(1, count + 1)
    )


def test_hard_overlap_policy_requires_real_intersection_evidence() -> None:
    request = prepare_request(project_config())
    parameters = request.parameter_set.model_copy(
        update={
            "overlap_policy": "DIRECTIONAL_CONTAINMENT",
            "overlap_threshold": 0.6,
        }
    )
    with pytest.raises(SeedingError) as exc:
        evaluate_audiences(
            request.audiences,
            parameters=parameters,
            overlap_evidence=(),
        )
    assert exc.value.code == "SOURCE_PROOF_MISSING"


def test_small_static_pool_is_marked_for_review_without_blocking_dynamic_pool() -> None:
    request = prepare_request(project_config())
    dynamic = AudienceCandidate(
        audience_id="dynamic-1",
        audience_mode="DYNAMIC_BEHAVIOR",
        source_system="JUGUANG",
        metric_state="READY",
        population=3_500_000,
        hard_gates={
            "availability": True,
            "size": True,
            "quality": True,
            "policy": True,
            "relevance": True,
        },
        asset=DynamicBehaviorTargeting(
            project_id=project_config().project_id,
            contract_version="seeding.dynamic_behavior_targeting.v1",
            config_sha256="b" * 64,
            source_proof_sha256=SHA,
            created_at=NOW,
            behavior_type="SEARCH",
            keyword_ids=("kw-1",),
            lookback_days=30,
            estimated_population=3_500_000,
            recomputed_at=NOW,
            template_id="behavior-template-v1",
            binding_mode="STANDALONE",
        ),
    )
    result = evaluate_audiences(
        request.audiences[:4] + (dynamic,),
        parameters=request.parameter_set,
    )
    assert result.eligible_audience_ids == ("dynamic-1",)
    static = [item for item in result.decisions if item.audience_mode == "STATIC_DMP"]
    assert all("SCORING_SAMPLE_INSUFFICIENT" in item.reason_codes for item in static)


def test_directional_overlap_rejects_lower_ranked_candidate() -> None:
    request = prepare_request(project_config())
    parameters = request.parameter_set.model_copy(
        update={
            "overlap_policy": "DIRECTIONAL_CONTAINMENT",
            "overlap_threshold": 0.6,
        }
    )
    evidence = []
    for left in range(1, 6):
        for right in range(left + 1, 6):
            intersection = 0
            if {left, right} == {4, 5}:
                intersection = min(
                    audience(left).population, audience(right).population
                )
            evidence.append(
                AudienceOverlapEvidence(
                    left_audience_id=f"aud-{left}",
                    right_audience_id=f"aud-{right}",
                    intersection=intersection,
                    source_proof_sha256=SHA,
                )
            )
    result = evaluate_audiences(
        request.audiences,
        parameters=parameters,
        overlap_evidence=tuple(evidence),
    )
    assert len(result.eligible_audience_ids) == 4
    rejected = [item for item in result.decisions if not item.eligible]
    assert len(rejected) == 1
    assert "AUDIENCE_OVERLAP_REJECTED" in rejected[0].reason_codes


def test_keyword_pipeline_normalizes_dedupes_and_computes_ocpx_evidence() -> None:
    request = prepare_request(project_config())
    rows = keywords() + (
        KeywordCandidate(
            keyword_id="kw-duplicate",
            raw_text="防晒霜 1！",
            normalized_text="防晒霜 1",
            primary_lane="CATEGORY",
            heat=9_000,
            suggested_bid_fen=200,
            source_round=1,
            normalizer_version="nfkc-safe-punctuation-v1",
            source_proof_sha256=SHA,
        ),
    )
    result = evaluate_keywords(
        rows,
        parameters=request.parameter_set,
        bid_mode="OCPX_STABLE_COST",
    )
    by_id = {item.keyword_id: item for item in result.decisions}
    assert by_id["kw-1"].eligible is True
    assert by_id["kw-1"].wire_bid_fen == 0
    assert by_id["kw-duplicate"].eligible is False
    assert "EXACT_DUPLICATE_SUPERSEDED" in by_id["kw-duplicate"].reason_codes
    assert result.eligible_by_lane["BRAND"] == (
        "kw-5",
        "kw-4",
        "kw-3",
        "kw-2",
        "kw-1",
    )


def test_keyword_without_wire_bid_is_not_eligible() -> None:
    request = prepare_request(project_config())
    rows = (
        KeywordCandidate(
            keyword_id="kw-no-bid",
            raw_text="防晒霜",
            normalized_text="防晒霜",
            primary_lane="BRAND",
            heat=9_000,
            suggested_bid_fen=None,
            source_round=1,
            normalizer_version="nfkc-safe-punctuation-v1",
            source_proof_sha256=SHA,
        ),
    )
    result = evaluate_keywords(
        rows,
        parameters=request.parameter_set,
        bid_mode="MANUAL_KEYWORD",
    )
    decision = result.decisions[0]
    assert decision.eligible is False
    assert decision.wire_bid_fen is None
    assert "BID_EVIDENCE_MISSING" in decision.reason_codes
    assert result.eligible_by_lane["BRAND"] == tuple()


def test_semantic_dedupe_requires_frozen_model_and_preserves_receipt() -> None:
    request = prepare_request(project_config())
    parameters = request.parameter_set.model_copy(
        update={
            "semantic_dedupe_enabled": True,
            "semantic_similarity_threshold": 0.9,
            "semantic_model_id": "embedding-v1",
        }
    )
    evidence = SemanticDuplicateEvidence(
        left_keyword_id="kw-1",
        right_keyword_id="kw-2",
        similarity=0.95,
        semantic_model_id="embedding-v1",
        prompt_version="prompt-v1",
        embedding_sha256=SHA,
    )
    result = evaluate_keywords(
        keywords(),
        parameters=parameters,
        semantic_evidence=(evidence,),
        bid_mode="OCPX_STABLE_COST",
    )
    by_id = {item.keyword_id: item for item in result.decisions}
    assert sum(item.eligible for item in result.decisions) == 4
    assert any(
        "SEMANTIC_DUPLICATE_SUPERSEDED" in item.reason_codes
        for item in (by_id["kw-1"], by_id["kw-2"])
    )


def test_search_release_gate_supports_cost_kpis_where_lower_is_better() -> None:
    request = prepare_request(project_config())
    parameters = request.parameter_set.model_copy(
        update={
            "search_release_gate": SearchReleaseGate(
                min_observation_days=3,
                min_spend_fen=10_000,
                min_impressions=1_000,
                min_primary_kpi_value=500,
                primary_kpi_comparison="LTE",
            )
        }
    )
    allowed = SearchReleaseEvidence(
        note_id="note-1",
        observation_days=3,
        spend_fen=20_000,
        impressions=10_000,
        primary_kpi_value=450,
        data_quality_ok=True,
        note_available=True,
        source_proof_sha256=SHA,
    )
    blocked = allowed.model_copy(update={"primary_kpi_value": 550})
    assert search_release_allowed(allowed, parameters=parameters)[0] is True
    result = search_release_allowed(blocked, parameters=parameters)
    assert result[0] is False
    assert result[1] == ("SEARCH_PRIMARY_KPI_ABOVE_GATE",)


def test_prepare_builds_search_plans_only_after_release_gate(tmp_path) -> None:
    base_config = project_config()
    payload = base_config.model_dump(mode="json", by_alias=True)
    payload["search"] = {"enabled": True}
    config = ProjectConfig.model_validate(payload)
    base_request = prepare_request(config)
    lane_pairs = tuple(
        PairCandidate(
            note_id=f"note-{index}",
            asset_id="BRAND",
            pair_type="NOTE_KEYWORD_LANE",
            pair_score=95,
            match_grade="A",
            evidence_spans=("confirmed search intent",),
            model_id="pair-model-v1",
            prompt_version="prompt-v1",
            rule_version="rule-v1",
        )
        for index in (1, 2)
    )
    release = tuple(
        SearchReleaseEvidence(
            note_id=f"note-{index}",
            observation_days=3,
            spend_fen=20_000,
            impressions=10_000,
            primary_kpi_value=2,
            data_quality_ok=True,
            note_available=True,
            source_proof_sha256=SHA,
        )
        for index in (1, 2)
    )
    request_payload = base_request.model_dump(mode="json", by_alias=True)
    request_payload.update(
        {
            "config_sha": sha256_json(config),
            "keywords": [
                item.model_dump(mode="json", by_alias=True) for item in keywords()
            ],
            "pairs": [
                item.model_dump(mode="json", by_alias=True)
                for item in base_request.pairs + lane_pairs
            ],
            "search_release_evidence": [
                item.model_dump(mode="json", by_alias=True) for item in release
            ],
        }
    )
    request = PrepareRequest.model_validate(request_payload)
    service = SeedingService(SeedingStore(tmp_path / "seeding.sqlite3"))
    service.create_project(config, principal_id="owner-1")
    service.enqueue_prepare(config.project_id, request, principal_id="owner-1")
    result = service.process_next_job(worker_id="test-worker")
    assert result is not None
    plans = result["content"]["plans"]
    assert sum(item["channel"] == "FEED" for item in plans) == 4
    search_plans = [item for item in plans if item["channel"] == "SEARCH"]
    assert len(search_plans) == 2
    assert all(len(item["keyword_ids"]) == 5 for item in search_plans)
    assert all(item["primary_lane"] == "BRAND" for item in search_plans)


def test_prepare_rejects_parameter_set_identity_drift_before_enqueuing(
    tmp_path,
) -> None:
    config = project_config()
    request = prepare_request(config)
    payload = request.model_dump(mode="json", by_alias=True)
    payload["parameter_set"]["version"] = 2
    drifted = PrepareRequest.model_validate(payload)
    service = SeedingService(SeedingStore(tmp_path / "seeding.sqlite3"))
    service.create_project(config, principal_id="owner-1")
    with pytest.raises(SeedingError) as exc:
        service.enqueue_prepare(config.project_id, drifted, principal_id="owner-1")
    assert exc.value.code == "PARAMETER_SET_MISMATCH"
    assert service.store.list_jobs(config.project_id) == []
