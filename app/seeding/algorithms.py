"""Deterministic, side-effect-free SEEDING algorithms."""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from .errors import SeedingError

NORMALIZER_VERSION = "nfkc-safe-punctuation-v1"
_WHITESPACE_RE = re.compile(r"\s+")


def quantile_type7(values: Sequence[float], probability: float) -> float:
    """Hyndman–Fan Type 7 quantile, matching common dataframe defaults."""

    if not values:
        raise ValueError("values must not be empty")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) for value in ordered):
        raise ValueError("values must be finite")
    h = (len(ordered) - 1) * probability
    lower = math.floor(h)
    upper = math.ceil(h)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (h - lower) * (ordered[upper] - ordered[lower])


def winsorize_type7(
    values: Sequence[float], low: float = 0.05, high: float = 0.95
) -> List[float]:
    lower = quantile_type7(values, low)
    upper = quantile_type7(values, high)
    return [min(max(float(value), lower), upper) for value in values]


def population_zscores(values: Sequence[float]) -> Tuple[List[float], bool]:
    if not values:
        raise ValueError("values must not be empty")
    mean = statistics.fmean(values)
    variance = statistics.fmean((value - mean) ** 2 for value in values)
    if variance == 0:
        return [0.0 for _ in values], True
    standard_deviation = math.sqrt(variance)
    return [(value - mean) / standard_deviation for value in values], False


@dataclass(frozen=True)
class StaticMetric:
    audience_id: str
    population: int
    aips: int
    i_ti: int


@dataclass(frozen=True)
class AudienceScore:
    audience_id: str
    penetration: float
    deep: float
    raw: float
    display: float
    reason_codes: Tuple[str, ...]


def score_static_audiences(
    rows: Sequence[StaticMetric],
    *,
    penetration_metric: str,
    penetration_weight: float,
    deep_weight: float,
) -> List[AudienceScore]:
    if len(rows) < 5:
        raise SeedingError(
            "SCORING_SAMPLE_INSUFFICIENT",
            "at least five comparable static DMP snapshots are required",
            details={"sample_size": len(rows)},
        )
    if penetration_metric not in {"PenAIPS", "PenTI"}:
        raise SeedingError(
            "SEEDING_CONFIG_REVIEW_REQUIRED",
            "penetration KPI mapping must be explicitly confirmed",
            details={"penetration_metric": penetration_metric},
        )
    if not math.isclose(penetration_weight + deep_weight, 1.0, abs_tol=1e-9):
        raise ValueError("score weights must sum to 1")

    penetration_values: List[float] = []
    deep_values: List[float] = []
    for row in rows:
        if not (row.population > 0 and 0 <= row.i_ti <= row.aips <= row.population):
            raise SeedingError(
                "AUDIENCE_METRIC_INCONSISTENT",
                "static DMP metrics must satisfy 0 <= I+TI <= AIPS <= N",
                details={
                    "audience_id": row.audience_id,
                    "N": row.population,
                    "A": row.aips,
                    "T": row.i_ti,
                },
            )
        if row.aips == 0:
            raise SeedingError(
                "AUDIENCE_METRIC_INCONSISTENT",
                "AIPS must be positive before Deep can be calculated",
                details={"audience_id": row.audience_id, "A": 0},
            )
        penetration_values.append(
            row.aips / row.population
            if penetration_metric == "PenAIPS"
            else row.i_ti / row.population
        )
        deep_values.append(row.i_ti / row.aips)

    penetration_winsorized = winsorize_type7(penetration_values)
    deep_winsorized = winsorize_type7(deep_values)
    penetration_z, penetration_zero = population_zscores(penetration_winsorized)
    deep_z, deep_zero = population_zscores(deep_winsorized)

    output: List[AudienceScore] = []
    for index, row in enumerate(rows):
        raw = penetration_weight * penetration_z[index] + deep_weight * deep_z[index]
        display = 100 * (0.5 * (1 + math.erf(raw / math.sqrt(2))))
        reasons: List[str] = []
        if penetration_zero:
            reasons.append("ZERO_VARIANCE_PENETRATION")
        if deep_zero:
            reasons.append("ZERO_VARIANCE_DEEP")
        output.append(
            AudienceScore(
                audience_id=row.audience_id,
                penetration=penetration_values[index],
                deep=deep_values[index],
                raw=raw,
                display=round(display, 2),
                reason_codes=tuple(reasons),
            )
        )
    return sorted(output, key=lambda item: (-item.raw, item.audience_id))


@dataclass(frozen=True)
class OverlapMetrics:
    contain_left_from_right: float
    contain_right_from_left: float
    jaccard: float


def overlap_metrics(
    *, left_population: int, right_population: int, intersection: int
) -> OverlapMetrics:
    if left_population <= 0 or right_population <= 0:
        raise ValueError("population must be positive")
    if not 0 <= intersection <= min(left_population, right_population):
        raise ValueError("intersection is outside the valid range")
    union = left_population + right_population - intersection
    return OverlapMetrics(
        contain_left_from_right=intersection / right_population,
        contain_right_from_left=intersection / left_population,
        jaccard=intersection / union,
    )


def normalize_keyword(text: str, *, safe_punctuation: str = "-+&/") -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().lower()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    allowed = set(safe_punctuation)
    kept = []
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith("P") and character not in allowed:
            continue
        kept.append(character)
    result = _WHITESPACE_RE.sub(" ", "".join(kept)).strip()
    if not result:
        raise ValueError("keyword is empty after normalization")
    return result


def keyword_heat_threshold(
    *,
    category_heat: int,
    comparable_category_heats: Sequence[int],
) -> Tuple[int, Tuple[str, ...]]:
    if category_heat < 0 or any(value < 0 for value in comparable_category_heats):
        raise ValueError("heat values must be non-negative")
    if len(comparable_category_heats) < 5:
        return 5000, ("KEYWORD_BENCHMARK_INSUFFICIENT", "REVIEW_REQUIRED")
    reference = statistics.median(comparable_category_heats)
    if reference <= 0:
        return 5000, ("KEYWORD_BENCHMARK_INSUFFICIENT", "REVIEW_REQUIRED")
    raw = 5000 * category_heat / reference
    clamped = min(max(raw, 1000), 5000)
    return int(round(clamped / 100.0) * 100), tuple()


@dataclass(frozen=True)
class BidEvidence:
    input_bid_fen: int
    baseline_fen: int
    raw_candidate_fen: float
    rounded_candidate_fen: int
    wire_bid_fen: int
    reason_codes: Tuple[str, ...]


def bid_evidence(
    *,
    bid_fen: int,
    peer_bids_fen: Sequence[int],
    min_bid_fen: int,
    max_bid_fen: int,
    bid_step_fen: int,
    bid_mode: str,
) -> BidEvidence:
    if bid_fen < 0 or len(peer_bids_fen) < 5:
        raise ValueError(
            "bid must be non-negative and at least five peers are required"
        )
    if not (0 <= min_bid_fen <= max_bid_fen and bid_step_fen > 0):
        raise ValueError("invalid platform bid bounds")
    peers = [int(value) for value in peer_bids_fen]
    p25 = quantile_type7(peers, 0.25)
    p50 = quantile_type7(peers, 0.50)
    p75 = quantile_type7(peers, 0.75)
    iqr = p75 - p25
    median = p50
    mad = statistics.median(abs(value - median) for value in peers)
    reasons: List[str] = []
    is_outlier = False
    if iqr > 0:
        is_outlier = bid_fen > p75 + 1.5 * iqr
        reasons.append("IQR_RULE")
    elif mad > 0:
        is_outlier = bid_fen > median + 3 * 1.4826 * mad
        reasons.append("MAD_FALLBACK")
    else:
        reasons.append("NO_OUTLIER_ZERO_SPREAD")
    baseline = int(round(p50)) if is_outlier else bid_fen
    if is_outlier:
        reasons.append("OUTLIER_REPLACED_WITH_P50")
    raw_candidate = baseline * 0.7
    rounded = math.floor(raw_candidate / bid_step_fen) * bid_step_fen
    rounded = min(max(rounded, min_bid_fen), max_bid_fen)
    if rounded != int(raw_candidate):
        reasons.append("PLATFORM_STEP_OR_BOUND_APPLIED")
    if bid_mode == "OCPX_STABLE_COST":
        wire_bid = 0
        reasons.append("OCPX_EVIDENCE_ONLY_WIRE_ZERO")
    elif bid_mode == "MANUAL_KEYWORD":
        wire_bid = rounded
    else:
        raise SeedingError(
            "PLATFORM_ENUM_UNVERIFIED",
            "bid_mode is not verified",
            details={"bid_mode": bid_mode},
        )
    return BidEvidence(
        input_bid_fen=bid_fen,
        baseline_fen=baseline,
        raw_candidate_fen=raw_candidate,
        rounded_candidate_fen=rounded,
        wire_bid_fen=wire_bid,
        reason_codes=tuple(reasons),
    )


@dataclass(frozen=True)
class CapacityResult:
    pair_cost_fen: int
    total_pair_slots: int
    audiences_per_note: int
    eligible_audiences_per_note: int
    selected_audiences_per_note: int
    unallocated_fen: int
    waiting_budget: bool


def uniform_budget_capacity(
    *,
    expected_daily_spend_fen: int,
    observation_days: int,
    phase_budget_fen: int,
    note_count: int,
    eligible_audiences_per_note: int,
    business_cap: int,
) -> CapacityResult:
    values = (
        expected_daily_spend_fen,
        observation_days,
        phase_budget_fen,
        note_count,
        eligible_audiences_per_note,
        business_cap,
    )
    if any(value <= 0 for value in values):
        raise ValueError("capacity inputs must all be positive")
    pair_cost = expected_daily_spend_fen * observation_days
    total_pair_slots = phase_budget_fen // pair_cost
    per_note = total_pair_slots // note_count
    selected = min(per_note, eligible_audiences_per_note, business_cap)
    committed = selected * note_count * pair_cost
    return CapacityResult(
        pair_cost_fen=pair_cost,
        total_pair_slots=total_pair_slots,
        audiences_per_note=per_note,
        eligible_audiences_per_note=eligible_audiences_per_note,
        selected_audiences_per_note=selected,
        unallocated_fen=phase_budget_fen - committed,
        waiting_budget=selected == 0,
    )
