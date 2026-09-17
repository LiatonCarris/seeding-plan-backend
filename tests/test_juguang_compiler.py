from __future__ import annotations

import pytest

from seeding_fixtures import (
    SHA,
    audience,
    compiled_prepare_request,
    platform_profile,
    prepare_request,
    project_config,
)
from test_seeding_decision_pipeline import keywords

from app.seeding.decision import evaluate_keywords
from app.seeding.errors import SeedingError
from app.seeding.juguang_compiler import compile_feed_plan, compile_search_plan
from app.seeding.service import SeedingService
from app.seeding.store import SeedingStore


def feed_plan() -> dict:
    return {
        "channel": "FEED",
        "note_id": "note-1",
        "audience_id": "aud-1",
        "daily_budget_fen": 10_000,
        "identity": {"logical_plan_key": SHA},
    }


def test_feed_compiler_maps_one_note_one_crowd_package() -> None:
    receipt = compile_feed_plan(
        project=project_config(),
        plan=feed_plan(),
        audience=audience(1),
        keywords_by_id={},
        profile=platform_profile(),
    )
    payload = receipt.payload.model_dump(mode="json", exclude_none=True)
    assert payload["advertiser_id"] == 12345
    cascade = payload["create_cascade_info_list"][0]
    assert cascade["campaign"]["marketing_target"] == 4
    assert cascade["campaign"]["placement"] == 1
    unit = cascade["unit_with_creative_list"][0]["unit"]
    assert unit["target_type"] == 3
    package = unit["target_info"]["crowd_target"]["crowd_pkg"][0]
    assert package == {
        "value": "pkg-1",
        "name": "Audience package 1",
        "group_id": "group-1",
    }
    assert receipt.payload_sha256


def test_compiler_rejects_unverified_enums() -> None:
    with pytest.raises(SeedingError) as exc:
        compile_feed_plan(
            project=project_config(),
            plan=feed_plan(),
            audience=audience(1),
            keywords_by_id={},
            profile=platform_profile(verified=False),
        )
    assert exc.value.code == "PLATFORM_ENUM_UNVERIFIED"


def test_search_compiler_maps_keywords_and_ocpx_wire_bid_zero() -> None:
    rows = keywords()
    request = prepare_request(project_config())
    decisions = evaluate_keywords(
        rows,
        parameters=request.parameter_set,
        bid_mode="OCPX_STABLE_COST",
    )
    plan = {
        "channel": "SEARCH",
        "note_id": "note-1",
        "primary_lane": "BRAND",
        "keyword_ids": tuple(item.keyword_id for item in rows),
        "daily_budget_fen": 10_000,
        "identity": {"logical_plan_key": SHA},
    }
    receipt = compile_search_plan(
        project=project_config(),
        plan=plan,
        keywords_by_id={item.keyword_id: item for item in rows},
        keyword_decisions={item.keyword_id: item for item in decisions.decisions},
        profile=platform_profile(),
    )
    cascade = receipt.payload.create_cascade_info_list[0]
    assert cascade.campaign.placement == 2
    assert cascade.unit_with_creative_list[0].unit.target_type == 0
    assert len(cascade.unit_with_creative_list[0].unit.keyword_with_bid) == 5
    assert all(
        item.bid == 0
        for item in cascade.unit_with_creative_list[0].unit.keyword_with_bid
    )


def test_prepare_embeds_exact_platform_payload_and_removes_enum_risk(tmp_path) -> None:
    config = project_config()
    request = compiled_prepare_request(config)
    service = SeedingService(SeedingStore(tmp_path / "seeding.sqlite3"))
    service.create_project(config, principal_id="owner-1")
    service.enqueue_prepare(config.project_id, request, principal_id="owner-1")
    result = service.process_next_job(worker_id="test-worker")
    assert result is not None
    assert "PLATFORM_ENUM_UNVERIFIED" not in result["content"]["risks"]
    assert all("platform_payload" in plan for plan in result["content"]["plans"])
    assert all(plan["platform_payload_sha256"] for plan in result["content"]["plans"])
