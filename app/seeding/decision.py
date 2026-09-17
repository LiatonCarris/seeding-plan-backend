"""Deterministic audience and keyword decision pipeline.

This module connects the previously isolated algorithms to the planning flow.
It never invents unavailable Lingxi metrics or semantic similarity evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from .algorithms import (
    AudienceScore,
    StaticMetric,
    bid_evidence,
    keyword_heat_threshold,
    normalize_keyword,
    overlap_metrics,
    score_static_audiences,
)
from .contracts import (
    LANE_PRIORITY,
    AudienceCandidate,
    AudienceOverlapEvidence,
    KeywordCandidate,
    SearchReleaseEvidence,
    SeedingParameterSet,
    SemanticDuplicateEvidence,
    StaticDmpSnapshot,
)
from .errors import SeedingError


@dataclass(frozen=True)
class AudienceDecision:
    audience_id: str
    audience_mode: str
    eligible: bool
    authoritative_score: float | None
    display_score: float | None
    reason_codes: Tuple[str, ...]


@dataclass(frozen=True)
class AudienceDecisionResult:
    decisions: Tuple[AudienceDecision, ...]
    eligible_audience_ids: Tuple[str, ...]
    static_scores: Tuple[AudienceScore, ...]
    risks: Tuple[str, ...]


@dataclass(frozen=True)
class KeywordDecision:
    keyword_id: str
    normalized_text: str
    primary_lane: str
    eligible: bool
    wire_bid_fen: int | None
    reason_codes: Tuple[str, ...]


@dataclass(frozen=True)
class KeywordDecisionResult:
    decisions: Tuple[KeywordDecision, ...]
    eligible_by_lane: Mapping[str, Tuple[str, ...]]
    risks: Tuple[str, ...]
    heat_threshold: int


def _require_frozen_parameters(parameters: SeedingParameterSet) -> None:
    if parameters.status != "FROZEN":
        raise SeedingError(
            "SEEDING_CONFIG_REVIEW_REQUIRED",
            "the parameter set is not frozen",
            details={
                "parameter_set_id": parameters.parameter_set_id,
                "version": parameters.version,
            },
        )


def evaluate_audiences(
    audiences: Sequence[AudienceCandidate],
    *,
    parameters: SeedingParameterSet,
    overlap_evidence: Sequence[AudienceOverlapEvidence] = (),
) -> AudienceDecisionResult:
    _require_frozen_parameters(parameters)
    minimum = parameters.audience_min_population
    assert minimum is not None
    base_reasons: Dict[str, List[str]] = {}
    base_eligible: Dict[str, bool] = {}
    by_id = {item.audience_id: item for item in audiences}

    for item in audiences:
        reasons: List[str] = []
        if item.metric_state != "READY":
            reasons.append(f"METRIC_STATE_{item.metric_state}")
        failed_gates = sorted(
            name for name, passed in item.hard_gates.items() if not passed
        )
        reasons.extend(f"HARD_GATE_{name.upper()}" for name in failed_gates)
        if item.population < minimum:
            reasons.append("AUDIENCE_BELOW_MIN_POPULATION")
        if item.population > parameters.audience_max_population:
            reasons.append("AUDIENCE_ABOVE_MAX_POPULATION")
        base_reasons[item.audience_id] = reasons
        base_eligible[item.audience_id] = not reasons

    static_candidates = [
        item
        for item in audiences
        if base_eligible[item.audience_id]
        and item.audience_mode == "STATIC_DMP"
        and isinstance(item.asset, StaticDmpSnapshot)
    ]
    static_scores: Tuple[AudienceScore, ...] = tuple()
    if static_candidates:
        try:
            static_scores = tuple(
                score_static_audiences(
                    [
                        StaticMetric(
                            audience_id=item.audience_id,
                            population=item.asset.population,
                            aips=item.asset.aips,
                            i_ti=item.asset.i_ti,
                        )
                        for item in static_candidates
                    ],
                    penetration_metric=str(parameters.penetration_metric),
                    penetration_weight=parameters.penetration_weight,
                    deep_weight=parameters.deep_weight,
                )
            )
        except SeedingError as exc:
            if exc.code != "SCORING_SAMPLE_INSUFFICIENT":
                raise
            for item in static_candidates:
                base_eligible[item.audience_id] = False
                base_reasons[item.audience_id].append("SCORING_SAMPLE_INSUFFICIENT")

    risks: Set[str] = set()
    score_by_id = {item.audience_id: item for item in static_scores}
    ranked_by_mode: Dict[str, List[str]] = {
        "STATIC_DMP": [item.audience_id for item in static_scores],
        "DYNAMIC_BEHAVIOR": sorted(
            item.audience_id
            for item in audiences
            if base_eligible[item.audience_id]
            and item.audience_mode == "DYNAMIC_BEHAVIOR"
        ),
    }

    evidence: Dict[Tuple[str, str], AudienceOverlapEvidence] = {}
    for row in overlap_evidence:
        key = tuple(sorted((row.left_audience_id, row.right_audience_id)))
        if key in evidence:
            raise SeedingError(
                "DUPLICATE_OVERLAP_EVIDENCE",
                "only one overlap receipt is allowed per audience pair",
                details={"pair": key},
            )
        evidence[key] = row

    selected: Set[str] = set()
    if parameters.overlap_policy == "WARNING_ONLY":
        selected.update(
            audience_id for audience_id, eligible in base_eligible.items() if eligible
        )
        if len(selected) > 1 and not overlap_evidence:
            risks.add("AUDIENCE_OVERLAP_UNVERIFIED")
    else:
        threshold = parameters.overlap_threshold
        assert threshold is not None
        for mode, ranked in ranked_by_mode.items():
            chosen_for_mode: List[str] = []
            for candidate_id in ranked:
                candidate = by_id[candidate_id]
                rejected = False
                for chosen_id in chosen_for_mode:
                    key = tuple(sorted((candidate_id, chosen_id)))
                    row = evidence.get(key)
                    if row is None:
                        raise SeedingError(
                            "SOURCE_PROOF_MISSING",
                            "hard overlap filtering requires pairwise intersection evidence",
                            details={
                                "left_audience_id": candidate_id,
                                "right_audience_id": chosen_id,
                                "audience_mode": mode,
                            },
                        )
                    metrics = overlap_metrics(
                        left_population=candidate.population,
                        right_population=by_id[chosen_id].population,
                        intersection=row.intersection,
                    )
                    if parameters.overlap_policy == "DIRECTIONAL_CONTAINMENT":
                        rejected = (
                            metrics.contain_left_from_right >= threshold
                            or metrics.contain_right_from_left >= threshold
                        )
                    else:
                        rejected = metrics.jaccard >= threshold
                    if rejected:
                        base_reasons[candidate_id].append("AUDIENCE_OVERLAP_REJECTED")
                        break
                if not rejected:
                    chosen_for_mode.append(candidate_id)
            selected.update(chosen_for_mode)

    decisions: List[AudienceDecision] = []
    for item in audiences:
        score = score_by_id.get(item.audience_id)
        eligible = base_eligible[item.audience_id] and item.audience_id in selected
        decisions.append(
            AudienceDecision(
                audience_id=item.audience_id,
                audience_mode=str(item.audience_mode),
                eligible=eligible,
                authoritative_score=score.raw if score else None,
                display_score=score.display if score else None,
                reason_codes=tuple(base_reasons[item.audience_id])
                + (score.reason_codes if score else tuple()),
            )
        )
    return AudienceDecisionResult(
        decisions=tuple(sorted(decisions, key=lambda item: item.audience_id)),
        eligible_audience_ids=tuple(sorted(selected)),
        static_scores=static_scores,
        risks=tuple(sorted(risks)),
    )


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def evaluate_keywords(
    keywords: Sequence[KeywordCandidate],
    *,
    parameters: SeedingParameterSet,
    semantic_evidence: Sequence[SemanticDuplicateEvidence] = (),
    bid_mode: str,
) -> KeywordDecisionResult:
    _require_frozen_parameters(parameters)
    threshold, threshold_reasons = keyword_heat_threshold(
        category_heat=parameters.keyword_category_heat,
        comparable_category_heats=parameters.keyword_comparable_category_heats,
    )
    risks: Set[str] = set(threshold_reasons)
    by_id = {item.keyword_id: item for item in keywords}
    reasons: Dict[str, List[str]] = {item.keyword_id: [] for item in keywords}

    for item in keywords:
        expected = normalize_keyword(item.raw_text)
        if item.normalized_text != expected:
            raise SeedingError(
                "KEYWORD_NORMALIZATION_MISMATCH",
                "keyword normalized_text does not match the frozen normalizer",
                details={"keyword_id": item.keyword_id},
            )
        if item.heat < threshold:
            reasons[item.keyword_id].append("KEYWORD_BELOW_HEAT_THRESHOLD")

    lane_rank = {lane: index for index, lane in enumerate(LANE_PRIORITY)}

    def preference(item: KeywordCandidate) -> Tuple[int, int, str]:
        return (lane_rank[str(item.primary_lane)], -item.heat, item.keyword_id)

    exact_groups: Dict[str, List[KeywordCandidate]] = {}
    for item in keywords:
        exact_groups.setdefault(item.normalized_text, []).append(item)
    representatives: Set[str] = set()
    for group in exact_groups.values():
        winner = min(group, key=preference)
        representatives.add(winner.keyword_id)
        for item in group:
            if item.keyword_id != winner.keyword_id:
                reasons[item.keyword_id].append("EXACT_DUPLICATE_SUPERSEDED")

    union_find = _UnionFind(representatives)
    if parameters.semantic_dedupe_enabled:
        evidence_model = parameters.semantic_model_id
        similarity_threshold = parameters.semantic_similarity_threshold
        assert evidence_model and similarity_threshold is not None
        for row in semantic_evidence:
            if row.semantic_model_id != evidence_model:
                raise SeedingError(
                    "SEMANTIC_MODEL_MISMATCH",
                    "semantic evidence model does not match the frozen parameter set",
                    details={"semantic_model_id": row.semantic_model_id},
                )
            if (
                row.left_keyword_id not in representatives
                or row.right_keyword_id not in representatives
            ):
                continue
            left = by_id[row.left_keyword_id]
            right = by_id[row.right_keyword_id]
            if str(left.primary_lane) != str(right.primary_lane):
                risks.add("SEMANTIC_CROSS_LANE_CONFLICT_REVIEW_REQUIRED")
                continue
            if row.similarity >= similarity_threshold:
                union_find.union(left.keyword_id, right.keyword_id)
        if len(representatives) > 1 and not semantic_evidence:
            risks.add("SEMANTIC_DEDUPE_EVIDENCE_MISSING")

    semantic_groups: Dict[str, List[KeywordCandidate]] = {}
    for keyword_id in representatives:
        semantic_groups.setdefault(union_find.find(keyword_id), []).append(
            by_id[keyword_id]
        )
    semantic_representatives: Set[str] = set()
    for group in semantic_groups.values():
        winner = min(group, key=preference)
        semantic_representatives.add(winner.keyword_id)
        for item in group:
            if item.keyword_id != winner.keyword_id:
                reasons[item.keyword_id].append("SEMANTIC_DUPLICATE_SUPERSEDED")

    eligible_by_lane: Dict[str, List[str]] = {lane: [] for lane in LANE_PRIORITY}
    wire_bids: Dict[str, int | None] = {}
    for lane in LANE_PRIORITY:
        lane_items = sorted(
            (
                item
                for item in keywords
                if str(item.primary_lane) == lane
                and item.keyword_id in semantic_representatives
                and not reasons[item.keyword_id]
            ),
            key=lambda item: (-item.heat, item.keyword_id),
        )
        peer_bids = [
            item.suggested_bid_fen
            for item in lane_items
            if item.suggested_bid_fen is not None
        ]
        for item in lane_items[: parameters.keyword_business_limit]:
            if item.suggested_bid_fen is None:
                wire_bids[item.keyword_id] = None
                reasons[item.keyword_id].append("BID_EVIDENCE_MISSING")
            elif len(peer_bids) < 5:
                wire_bids[item.keyword_id] = (
                    0 if bid_mode == "OCPX_STABLE_COST" else None
                )
                reasons[item.keyword_id].append("BID_PEER_SAMPLE_INSUFFICIENT")
            else:
                evidence = bid_evidence(
                    bid_fen=item.suggested_bid_fen,
                    peer_bids_fen=[int(value) for value in peer_bids],
                    min_bid_fen=parameters.min_bid_fen,
                    max_bid_fen=parameters.max_bid_fen,
                    bid_step_fen=parameters.bid_step_fen,
                    bid_mode=bid_mode,
                )
                wire_bids[item.keyword_id] = evidence.wire_bid_fen
                reasons[item.keyword_id].extend(evidence.reason_codes)
            if wire_bids[item.keyword_id] is not None:
                eligible_by_lane[lane].append(item.keyword_id)
        for item in lane_items[parameters.keyword_business_limit :]:
            reasons[item.keyword_id].append("KEYWORD_BUSINESS_LIMIT_EXCEEDED")

    decisions = tuple(
        KeywordDecision(
            keyword_id=item.keyword_id,
            normalized_text=item.normalized_text,
            primary_lane=str(item.primary_lane),
            eligible=item.keyword_id
            in {value for values in eligible_by_lane.values() for value in values},
            wire_bid_fen=wire_bids.get(item.keyword_id),
            reason_codes=tuple(reasons[item.keyword_id]),
        )
        for item in sorted(keywords, key=lambda value: value.keyword_id)
    )
    return KeywordDecisionResult(
        decisions=decisions,
        eligible_by_lane={key: tuple(value) for key, value in eligible_by_lane.items()},
        risks=tuple(sorted(risks)),
        heat_threshold=threshold,
    )


def search_release_allowed(
    evidence: SearchReleaseEvidence,
    *,
    parameters: SeedingParameterSet,
) -> Tuple[bool, Tuple[str, ...]]:
    gate = parameters.search_release_gate
    if gate is None:
        return False, ("SEARCH_RELEASE_GATE_NOT_FROZEN",)
    reasons: List[str] = []
    if not evidence.note_available:
        reasons.append("SEARCH_NOTE_UNAVAILABLE")
    if not evidence.data_quality_ok:
        reasons.append("SEARCH_DATA_QUALITY_INVALID")
    if evidence.observation_days < gate.min_observation_days:
        reasons.append("SEARCH_OBSERVATION_WINDOW_INSUFFICIENT")
    if evidence.spend_fen < gate.min_spend_fen:
        reasons.append("SEARCH_SPEND_INSUFFICIENT")
    if evidence.impressions < gate.min_impressions:
        reasons.append("SEARCH_IMPRESSIONS_INSUFFICIENT")
    if (
        gate.primary_kpi_comparison == "GTE"
        and evidence.primary_kpi_value < gate.min_primary_kpi_value
    ):
        reasons.append("SEARCH_PRIMARY_KPI_BELOW_GATE")
    if (
        gate.primary_kpi_comparison == "LTE"
        and evidence.primary_kpi_value > gate.min_primary_kpi_value
    ):
        reasons.append("SEARCH_PRIMARY_KPI_ABOVE_GATE")
    return not reasons, tuple(reasons)
