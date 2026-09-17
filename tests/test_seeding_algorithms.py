from __future__ import annotations

import math

import pytest

from app.seeding.algorithms import (
    StaticMetric,
    bid_evidence,
    keyword_heat_threshold,
    normalize_keyword,
    overlap_metrics,
    quantile_type7,
    score_static_audiences,
    uniform_budget_capacity,
)
from app.seeding.errors import SeedingError


def test_type7_quantile_is_hand_calculable() -> None:
    assert quantile_type7([0, 10, 20, 30, 40], 0.25) == 10
    assert quantile_type7([0, 10, 20, 30], 0.25) == 7.5


def test_directional_overlap_uses_each_population_as_denominator() -> None:
    result = overlap_metrics(left_population=100, right_population=40, intersection=20)
    assert result.contain_left_from_right == 0.5
    assert result.contain_right_from_left == 0.2
    assert math.isclose(result.jaccard, 1 / 6)


def test_static_score_rejects_small_samples() -> None:
    with pytest.raises(SeedingError) as exc:
        score_static_audiences(
            [StaticMetric("a", 100, 20, 10)] * 4,
            penetration_metric="PenAIPS",
            penetration_weight=0.7,
            deep_weight=0.3,
        )
    assert exc.value.code == "SCORING_SAMPLE_INSUFFICIENT"


def test_static_score_marks_zero_variance_and_is_deterministic() -> None:
    rows = [StaticMetric(str(index), 1000, 100, 10) for index in range(5)]
    scores = score_static_audiences(
        rows,
        penetration_metric="PenAIPS",
        penetration_weight=0.7,
        deep_weight=0.3,
    )
    assert [score.raw for score in scores] == [0.0] * 5
    assert all("ZERO_VARIANCE_PENETRATION" in score.reason_codes for score in scores)
    assert all(score.display == 50.0 for score in scores)


def test_static_score_fails_closed_when_t_exceeds_a() -> None:
    rows = [StaticMetric(str(index), 1000, 100, 10) for index in range(4)]
    rows.append(StaticMetric("bad", 1000, 100, 101))
    with pytest.raises(SeedingError) as exc:
        score_static_audiences(
            rows,
            penetration_metric="PenTI",
            penetration_weight=0.7,
            deep_weight=0.3,
        )
    assert exc.value.code == "AUDIENCE_METRIC_INCONSISTENT"


def test_keyword_normalization_and_conservative_heat_fallback() -> None:
    assert normalize_keyword("  ＡＢＣ，  防晒  ") == "abc 防晒"
    threshold, reasons = keyword_heat_threshold(
        category_heat=1000,
        comparable_category_heats=[1000, 2000, 3000, 4000],
    )
    assert threshold == 5000
    assert reasons == ("KEYWORD_BENCHMARK_INSUFFICIENT", "REVIEW_REQUIRED")


def test_ocpx_bid_is_evidence_only_and_wire_value_is_zero() -> None:
    result = bid_evidence(
        bid_fen=900,
        peer_bids_fen=[100, 100, 100, 150, 200],
        min_bid_fen=50,
        max_bid_fen=1000,
        bid_step_fen=10,
        bid_mode="OCPX_STABLE_COST",
    )
    assert result.baseline_fen == 100
    assert result.rounded_candidate_fen == 70
    assert result.wire_bid_fen == 0
    assert "OUTLIER_REPLACED_WITH_P50" in result.reason_codes


def test_capacity_budget_insufficient_returns_k_zero() -> None:
    result = uniform_budget_capacity(
        expected_daily_spend_fen=10_000,
        observation_days=3,
        phase_budget_fen=59_999,
        note_count=2,
        eligible_audiences_per_note=5,
        business_cap=10,
    )
    assert result.total_pair_slots == 1
    assert result.audiences_per_note == 0
    assert result.selected_audiences_per_note == 0
    assert result.waiting_budget is True


def test_lower_budget_never_increases_capacity() -> None:
    inputs = dict(
        expected_daily_spend_fen=10_000,
        observation_days=3,
        note_count=2,
        eligible_audiences_per_note=5,
        business_cap=10,
    )
    high = uniform_budget_capacity(phase_budget_fen=180_000, **inputs)
    low = uniform_budget_capacity(phase_budget_fen=120_000, **inputs)
    assert low.selected_audiences_per_note <= high.selected_audiences_per_note
