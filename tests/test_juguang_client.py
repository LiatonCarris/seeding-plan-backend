from __future__ import annotations

import httpx
import pytest

from app.seeding.errors import SeedingError
from app.seeding.juguang import (
    AvailableTargetInfo,
    CrowdEstimateRequest,
    CrowdEstimateTargetConfig,
    JuguangClient,
    JuguangWriteGuard,
    KeywordRecommendRequest,
)


def client_for(handler, *, guard: JuguangWriteGuard | None = None) -> JuguangClient:
    transport = httpx.MockTransport(handler)
    return JuguangClient(
        token_provider=lambda: "runtime-only-token",
        http_client=httpx.Client(transport=transport),
        write_guard=guard,
    )


def test_available_targets_filter_only_synced_successful_packages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/target/get_available_target_info")
        assert request.headers["Access-Token"] == "runtime-only-token"
        payload = __import__("json").loads(request.content)
        assert payload == {"advertiser_id": 123, "marketing_target": 4}
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "成功",
                "success": True,
                "data": {
                    "industry_interest_target": {
                        "content_interests": [],
                        "shopping_interests": [],
                        "crowd_target": {
                            "dmp_permission": True,
                            "crowd_pkg": [
                                {
                                    "value": "2048_1",
                                    "name": "ready",
                                    "group_id": "1",
                                    "sync_status": 1,
                                    "status": 2,
                                },
                                {
                                    "value": "2048_2",
                                    "name": "not-synced",
                                    "group_id": "2",
                                    "sync_status": 0,
                                    "status": 2,
                                },
                                {
                                    "value": "2048_3",
                                    "name": "failed",
                                    "group_id": "3",
                                    "sync_status": 1,
                                    "status": 3,
                                },
                            ],
                        },
                    }
                },
            },
        )

    with client_for(handler) as client:
        result = client.get_available_target_info(advertiser_id=123)
    assert [item.value for item in result.deliverable_crowd_packages()] == ["2048_1"]


def test_available_targets_support_current_top_level_v1_shape() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "成功",
                "success": True,
                "data": {
                    "industry_interest_target": {
                        "content_interests": [],
                        "shopping_interests": [],
                    },
                    "crowd_target": {
                        "crowd_pkg": [
                            {
                                "value": "legacy-global",
                                "name": "legacy-global",
                                "sync_status": 1,
                                "status": 2,
                            }
                        ]
                    },
                    "crowd_target_v1": {
                        "customized_crowd_pkg": [
                            {
                                "value": "custom-ready",
                                "name": "custom-ready",
                                "group_id": "20880783",
                                "sync_status": 1,
                                "status": 2,
                            },
                            {
                                "value": "custom-expired",
                                "name": "custom-expired",
                                "sync_status": 1,
                                "status": 4,
                            },
                        ],
                        "market_crowd_pkg": [
                            {"value": "market", "name": "market", "children": []}
                        ],
                    },
                },
            },
        )

    with client_for(handler) as client:
        result = client.get_available_target_info(advertiser_id=123)

    assert [item.value for item in result.deliverable_crowd_packages()] == [
        "custom-ready"
    ]


def test_available_targets_do_not_fall_back_when_v1_customized_list_is_empty() -> None:
    parsed = AvailableTargetInfo.model_validate(
        {
            "industry_interest_target": {},
            "crowd_target": {
                "crowd_pkg": [
                    {
                        "value": "legacy-global",
                        "name": "legacy-global",
                        "sync_status": 1,
                        "status": 2,
                    }
                ]
            },
            "crowd_target_v1": {"customized_crowd_pkg": []},
        }
    )

    assert parsed.deliverable_crowd_packages() == ()


def test_crowd_estimate_contract_and_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert payload["placement"] == 1
        assert (
            payload["target_config"]["crowd_target"]["crowd_pkg"][0]["value"]
            == "2048_1"
        )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "success": True,
                "msg": "成功",
                "request_id": "request-1",
                "data": {
                    "crowd_scope": 2,
                    "crowd_num": "320 w",
                    "raw_crowd_num": 3_200_000,
                },
            },
        )

    request = CrowdEstimateRequest(
        advertiser_id=123,
        placement=1,
        optimize_target=1,
        target_type=3,
        target_config=CrowdEstimateTargetConfig(
            crowd_target={
                "crowd_pkg": [
                    {
                        "value": "2048_1",
                        "name": "ready",
                        "group_id": "1",
                        "sync_status": 1,
                        "status": 2,
                    }
                ]
            }
        ),
    )
    with client_for(handler) as client:
        result = client.estimate_crowd(request)
    assert result.raw_crowd_num == 3_200_000
    assert result.crowd_scope == 2


def test_keyword_recommendation_requires_request_specific_fields() -> None:
    with pytest.raises(ValueError):
        KeywordRecommendRequest(advertiser_id=123, request_type="search")
    with pytest.raises(ValueError):
        KeywordRecommendRequest(advertiser_id=123, request_type="note")

    request = KeywordRecommendRequest(
        advertiser_id=123,
        request_type="search",
        keyword="防晒",
        rank=1,
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(http_request.content)
        assert payload["request_type"] == "search"
        assert payload["keyword"] == "防晒"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "success": True,
                "msg": "成功",
                "data": {
                    "bag_month_pv": 10000,
                    "word_num": 1,
                    "word_list": [
                        {
                            "keyword": "防晒霜",
                            "source": 3,
                            "bid": 120,
                            "competition_level": "高",
                            "recommend_reason": ["高点击"],
                            "monthpv": 9000,
                        }
                    ],
                },
            },
        )

    with client_for(handler) as client:
        result = client.recommend_keywords(request)
    assert result.word_list[0].keyword == "防晒霜"
    assert result.word_list[0].bid == 120


def test_platform_error_does_not_expose_access_token() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 40001,
                "success": False,
                "msg": "invalid token runtime-only-token",
                "request_id": "request-2",
            },
        )

    with client_for(handler) as client:
        with pytest.raises(SeedingError) as exc:
            client.get_available_target_info(advertiser_id=123)
    assert exc.value.code == "PLATFORM_BUSINESS_ERROR"
    assert "runtime-only-token" not in str(exc.value.as_dict())
    assert "[REDACTED]" in str(exc.value.as_dict())


@pytest.mark.parametrize(
    "body",
    [
        {"unexpected": "envelope"},
        {"code": 0, "success": True, "msg": "成功", "data": "not-an-object"},
    ],
)
def test_malformed_platform_response_is_normalized(body: dict[str, object]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with client_for(handler) as client:
        with pytest.raises(SeedingError) as exc:
            client.get_available_target_info(advertiser_id=123)
    assert exc.value.code == "PLATFORM_SCHEMA_ERROR"
    assert exc.value.status_code == 502
    assert "runtime-only-token" not in str(exc.value.as_dict())


def test_write_is_locked_without_explicit_test_guard() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be reached")

    with client_for(handler) as client:
        with pytest.raises(SeedingError) as exc:
            client.create_cascade(
                advertiser_id=123,
                payload={"advertiser_id": 123, "create_type": 1},
            )
    assert exc.value.code == "FORMAL_EXECUTION_LOCKED"


def test_test_write_guard_checks_allowlist_and_parses_create_ids() -> None:
    guard = JuguangWriteGuard(
        enabled=True, environment="TEST", advertiser_allowlist=(123,)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/cascade/create")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "success": True,
                "msg": "成功",
                "data": {
                    "info_list": [
                        {
                            "campaign": {"campaign_id": 1001},
                            "unit_with_creative_list": [
                                {
                                    "unit": {"unit_id": 2001},
                                    "creativity_list": [{"creativity_id": 3001}],
                                }
                            ],
                        }
                    ]
                },
            },
        )

    with client_for(handler, guard=guard) as client:
        result = client.create_cascade(
            advertiser_id=123,
            payload={
                "advertiser_id": 123,
                "create_type": 1,
                "create_cascade_info_list": [],
            },
        )
    assert result.info_list[0]["campaign"]["campaign_id"] == 1001

    with client_for(handler, guard=guard) as client:
        with pytest.raises(SeedingError) as exc:
            client.create_cascade(
                advertiser_id=999,
                payload={"advertiser_id": 999, "create_type": 1},
            )
    assert exc.value.code == "ACCOUNT_NOT_ALLOWLISTED"
