from __future__ import annotations

import json
from datetime import timedelta

import httpx

from seeding_fixtures import NOW, SHA, project_config

from app.seeding.contracts import (
    JuguangKeywordSeed,
    JuguangSourceSyncRequest,
    LingxiAudienceImportRequest,
    LingxiAudienceMetric,
)
from app.seeding.juguang import JuguangClient
from app.seeding.service import SeedingService
from app.seeding.store import SeedingStore


def source_client() -> JuguangClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content)
        if path.endswith("/target/get_available_target_info"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "success": True,
                    "data": {
                        "industry_interest_target": {
                            "crowd_target": {
                                "crowd_pkg": [
                                    {
                                        "value": "pkg-1",
                                        "name": "Audience 1",
                                        "group_id": "group-1",
                                        "sync_status": 1,
                                        "status": 2,
                                    },
                                    {
                                        "value": "pkg-bad",
                                        "name": "Unavailable",
                                        "group_id": "group-bad",
                                        "sync_status": 0,
                                        "status": 2,
                                    },
                                ]
                            }
                        }
                    },
                },
            )
        if path.endswith("/crowd/estimate"):
            assert (
                body["target_config"]["crowd_target"]["crowd_pkg"][0]["value"]
                == "pkg-1"
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "success": True,
                    "data": {
                        "crowd_scope": 2,
                        "crowd_num": "320w",
                        "raw_crowd_num": 3_200_000,
                    },
                },
            )
        if path.endswith("/keyword/common/recommend"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "success": True,
                    "data": {
                        "bag_month_pv": 9000,
                        "word_num": 1,
                        "word_list": [
                            {
                                "keyword": "防晒霜！",
                                "bid": 230,
                                "monthpv": 9000,
                                "recommend_reason": ["高相关"],
                            }
                        ],
                    },
                },
            )
        if path.endswith("/keyword/word/bag/list"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "success": True,
                    "data": {
                        "page": {"page_num": 1, "total_count": 1},
                        "word_tag_dto_list": [{"name": "行业词包", "word_list": []}],
                    },
                },
            )
        raise AssertionError(path)

    return JuguangClient(
        token_provider=lambda: "runtime-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_juguang_sync_normalizes_keywords_and_keeps_lingxi_metrics_separate(
    tmp_path,
) -> None:
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    service = SeedingService(store, juguang_read_client=source_client())
    config = project_config()
    service.create_project(config, principal_id="owner-1")
    request = JuguangSourceSyncRequest(
        audience_estimate_optimize_target=1,
        audience_package_values=("pkg-1",),
        keyword_seeds=(
            JuguangKeywordSeed(
                primary_lane="CATEGORY",
                request_type="search",
                keyword="防晒",
            ),
        ),
    )
    job = service.enqueue_juguang_sync(
        config.project_id, request, principal_id="owner-1"
    )
    result = service.process_next_job(worker_id="source-worker")
    assert result is not None
    content = result["content"]
    assert job["kind"] == "SYNC_JUGUANG"
    assert [item["value"] for item in content["audience_catalog"]] == ["pkg-1"]
    assert content["audience_estimates"][0]["estimated_population"] == 3_200_000
    assert content["keywords"][0]["normalized_text"] == "防晒霜"
    assert content["keywords"][0]["primary_lane"] == "CATEGORY"
    assert "LINGXI_AIPS_I_TI_METRICS_REQUIRED" in content["warnings"]
    assert content["word_bags"][0]["name"] == "行业词包"

    imported = service.import_lingxi_audience_metrics(
        config.project_id,
        LingxiAudienceImportRequest(
            source_ref="lingxi-export-2026-09-17.csv",
            rows=(
                LingxiAudienceMetric(
                    package_value="pkg-1",
                    package_name="Audience 1",
                    group_id="group-1",
                    population=3_200_000,
                    aips=320_000,
                    **{"I+TI": 32_000},
                    snapshot_at=NOW,
                    expires_at=NOW + timedelta(days=1),
                    taxonomy_version="2026-09",
                    source_proof_sha256=SHA,
                ),
            ),
        ),
        principal_id="owner-1",
    )
    audience = imported["content"]["audiences"][0]
    assert audience["metric_state"] == "READY"
    assert audience["asset"]["I+TI"] == 32_000
    assert audience["asset"]["package_id"] == "pkg-1"
