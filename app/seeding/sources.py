"""Source adapters that normalize Juguang reads without inventing Lingxi metrics."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from .algorithms import NORMALIZER_VERSION, normalize_keyword
from .contracts import JuguangSourceSyncRequest
from .errors import SeedingError
from .identity import sha256_json
from .juguang import (
    CrowdEstimateRequest,
    CrowdEstimateTargetConfig,
    CrowdTarget,
    JuguangClient,
    KeywordRecommendRequest,
    make_source_proof,
)


def sync_juguang_sources(
    *,
    client: JuguangClient,
    advertiser_id: int,
    account_scope: str,
    request: JuguangSourceSyncRequest,
) -> Dict[str, Any]:
    retrieved_at = datetime.now(timezone.utc).isoformat()
    targets = client.get_available_target_info(
        advertiser_id=advertiser_id,
        marketing_target=request.marketing_target,
    )
    deliverable = targets.deliverable_crowd_packages()
    by_value = {item.value: item for item in deliverable}
    missing = sorted(set(request.audience_package_values) - set(by_value))
    if missing:
        raise SeedingError(
            "AUDIENCE_PACKAGE_NOT_DELIVERABLE",
            "requested audience packages are not synchronized and successful",
            status_code=409,
            details={"package_values": missing[:50]},
        )
    target_payload = targets.model_dump(mode="json", exclude_none=True)
    target_proof = make_source_proof(
        endpoint="/api/open/jg/target/get_available_target_info",
        request_parameters={
            "advertiser_id": advertiser_id,
            "marketing_target": request.marketing_target,
        },
        account_scope=account_scope,
        retrieved_at=retrieved_at,
        raw_payload=target_payload,
        normalized_payload=[
            item.model_dump(mode="json", exclude_none=True) for item in deliverable
        ],
    )
    estimates = []
    for value in request.audience_package_values:
        package = by_value[value]
        estimate_request = CrowdEstimateRequest(
            advertiser_id=advertiser_id,
            marketing_target=request.marketing_target,
            placement=request.audience_estimate_placement,
            optimize_target=request.audience_estimate_optimize_target,
            target_type=3,
            target_config=CrowdEstimateTargetConfig(
                crowd_target=CrowdTarget(crowd_pkg=(package,))
            ),
        )
        estimate = client.estimate_crowd(estimate_request)
        estimate_payload = estimate.model_dump(mode="json", exclude_none=True)
        proof = make_source_proof(
            endpoint="/api/open/jg/crowd/estimate",
            request_parameters=estimate_request.model_dump(
                mode="json", exclude_none=True
            ),
            account_scope=account_scope,
            retrieved_at=retrieved_at,
            raw_payload=estimate_payload,
            normalized_payload=estimate_payload,
        )
        estimates.append(
            {
                "package_value": package.value,
                "package_name": package.name,
                "group_id": package.group_id,
                "estimated_population": estimate.raw_crowd_num,
                "crowd_scope": estimate.crowd_scope,
                "source_proof": proof.__dict__,
                "source_proof_sha256": sha256_json(proof.__dict__),
            }
        )

    keyword_rows: Dict[str, Dict[str, Any]] = {}
    keyword_proofs = []
    for seed in request.keyword_seeds:
        api_request = KeywordRecommendRequest(
            advertiser_id=advertiser_id,
            request_type=seed.request_type,
            promotion_target=seed.promotion_target,
            keyword=seed.keyword,
            item_ids=seed.item_ids,
            taxonomy_id=seed.taxonomy_id,
            rank=seed.rank,
        )
        result = client.recommend_keywords(api_request)
        result_payload = result.model_dump(mode="json", exclude_none=True)
        proof = make_source_proof(
            endpoint="/api/open/jg/keyword/common/recommend",
            request_parameters=api_request.model_dump(mode="json", exclude_none=True),
            account_scope=account_scope,
            retrieved_at=retrieved_at,
            raw_payload=result_payload,
            normalized_payload=result_payload,
        )
        proof_sha = sha256_json(proof.__dict__)
        keyword_proofs.append({**proof.__dict__, "proof_sha256": proof_sha})
        for item in result.word_list:
            normalized = normalize_keyword(item.keyword)
            keyword_id = (
                "kw_"
                + sha256_json(
                    {"lane": str(seed.primary_lane), "normalized_text": normalized}
                )[:24]
            )
            candidate = {
                "keyword_id": keyword_id,
                "raw_text": item.keyword,
                "normalized_text": normalized,
                "primary_lane": str(seed.primary_lane),
                "secondary_tags": tuple(item.recommend_reason),
                "heat": item.monthpv,
                "suggested_bid_fen": item.bid,
                "source_round": seed.source_round,
                "normalizer_version": NORMALIZER_VERSION,
                "parent_keyword_id": seed.parent_keyword_id,
                "source_proof_sha256": proof_sha,
            }
            existing = keyword_rows.get(keyword_id)
            if existing is None or (
                candidate["heat"],
                candidate["suggested_bid_fen"] or 0,
            ) > (existing["heat"], existing["suggested_bid_fen"] or 0):
                keyword_rows[keyword_id] = candidate

    word_bags = []
    if request.include_word_bags:
        for page_num in range(1, request.word_bag_max_pages + 1):
            page = client.list_word_bags(
                advertiser_id=advertiser_id, page_num=page_num, page_size=5
            )
            word_bags.extend(
                item.model_dump(mode="json", exclude_none=True)
                for item in page.word_tag_dto_list
            )
            if page_num * 5 >= page.page.total_count:
                break

    return {
        "schema": "seeding.juguang_source_sync.v1",
        "retrieved_at": retrieved_at,
        "advertiser_id": advertiser_id,
        "audience_catalog": [
            item.model_dump(mode="json", exclude_none=True) for item in deliverable
        ],
        "audience_catalog_proof": target_proof.__dict__,
        "audience_estimates": estimates,
        "keywords": [keyword_rows[key] for key in sorted(keyword_rows)],
        "keyword_proofs": keyword_proofs,
        "word_bags": word_bags,
        "warnings": [
            "LINGXI_AIPS_I_TI_METRICS_REQUIRED",
            "AUDIENCE_INTERSECTION_EVIDENCE_REQUIRED_FOR_HARD_DEDUPE",
        ],
    }
