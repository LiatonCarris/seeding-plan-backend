"""Typed Xiaohongshu Juguang MAPI adapter with fail-closed write guards.

The adapter intentionally keeps authentication, transport, DTO validation, and
business authorization separate.  Access tokens are injected at runtime and
are never persisted in source artifacts or error details.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Mapping, Optional, Sequence, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import SeedingError
from .identity import sha256_json

JUGUANG_API_BASE_URL = "https://adapi.xiaohongshu.com"
JUGUANG_REPORT_BASE_URL = "https://ad.xiaohongshu.com"

AVAILABLE_TARGET_INFO_PATH = "/api/open/jg/target/get_available_target_info"
CROWD_ESTIMATE_PATH = "/api/open/jg/crowd/estimate"
KEYWORD_RECOMMEND_PATH = "/api/open/jg/keyword/common/recommend"
WORD_BAG_LIST_PATH = "/api/open/jg/keyword/word/bag/list"
TARGET_KEYWORD_RECOMMEND_PATH = "/api/open/jg/target/keyword/recommend"
CASCADE_CREATE_PATH = "/api/open/jg/cascade/create"
CAMPAIGN_STATUS_UPDATE_PATH = "/api/open/jg/campaign/status/update"
UNIT_LIST_PATH = "/api/open/jg/unit/list"
CAMPAIGN_LIST_PATH = "/api/open/jg/campaign/list"
CREATIVITY_SEARCH_PATH = "/api/open/jg/creativity/search"
GROUP_REPORT_V2_PATH = "/api/idea/group_report_v2"


class JuguangModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)


class CodeNamePair(JuguangModel):
    code: str
    name: str
    description: Optional[str] = None
    children: Tuple["CodeNamePair", ...] = tuple()


class CrowdPackage(JuguangModel):
    value: str
    name: str
    group_id: Optional[str] = None
    sync_status: Optional[int] = None
    status: Optional[int] = None
    type: Optional[str] = None
    tag: Optional[str] = None
    desc: Optional[str] = None

    @property
    def is_deliverable(self) -> bool:
        return self.sync_status == 1 and self.status == 2


class CrowdTarget(JuguangModel):
    crowd_pkg: Tuple[CrowdPackage, ...] = tuple()
    dmp_permission: Optional[bool] = None


class CrowdTargetV1(JuguangModel):
    customized_crowd_pkg: Tuple[CrowdPackage, ...] = tuple()
    market_crowd_pkg: Tuple[Dict[str, Any], ...] = tuple()


class IndustryInterestTarget(JuguangModel):
    content_interests: Tuple[CodeNamePair, ...] = tuple()
    shopping_interests: Tuple[CodeNamePair, ...] = tuple()
    crowd_target: Optional[CrowdTarget] = None


class AvailableTargetInfo(JuguangModel):
    industry_interest_target: IndustryInterestTarget
    crowd_target: Optional[CrowdTarget] = None
    crowd_target_v1: Optional[CrowdTargetV1] = None
    gender_targets: Tuple[CodeNamePair, ...] = tuple()
    age_targets: Tuple[CodeNamePair, ...] = tuple()
    area_targets: Tuple[CodeNamePair, ...] = tuple()
    area_targets_with_county: Tuple[CodeNamePair, ...] = tuple()
    device_targets: Tuple[CodeNamePair, ...] = tuple()

    def deliverable_crowd_packages(self) -> Tuple[CrowdPackage, ...]:
        # Current MAPI responses expose advertiser-owned DMP packages under the
        # top-level crowd_target_v1.customized_crowd_pkg field.  Older responses
        # used either top-level crowd_target or the industry-interest nesting.
        # Prefer the most specific current field so market/global packages are
        # not mistaken for advertiser-owned custom audiences.
        candidates: Tuple[CrowdPackage, ...] = tuple()
        if self.crowd_target_v1 is not None:
            candidates = self.crowd_target_v1.customized_crowd_pkg
        elif self.crowd_target is not None:
            candidates = self.crowd_target.crowd_pkg
        elif self.industry_interest_target.crowd_target is not None:
            candidates = self.industry_interest_target.crowd_target.crowd_pkg
        return tuple(item for item in candidates if item.is_deliverable)


class CrowdEstimateTargetConfig(JuguangModel):
    target_gender: str = "all"
    target_age: str = "all"
    target_city: str = "all"
    target_area_code: str = "-1"
    target_device: str = "all"
    industry_interest_target: Optional[Dict[str, Any]] = None
    crowd_target: Optional[CrowdTarget] = None
    interest_keywords: Tuple[str, ...] = tuple()
    keywords: Tuple[str, ...] = tuple()
    keyword_target_period: Optional[Literal[3, 7, 15, 30]] = None
    keyword_target_action: Tuple[Literal[1, 2, 3], ...] = tuple()

    @model_validator(mode="after")
    def validate_behavior_targeting(self) -> "CrowdEstimateTargetConfig":
        if self.keywords and (
            self.keyword_target_period is None or not self.keyword_target_action
        ):
            raise ValueError(
                "keyword behavior targeting requires period and at least one action"
            )
        if not self.keywords and (
            self.keyword_target_period is not None or self.keyword_target_action
        ):
            raise ValueError("keyword period/actions require behavior keywords")
        return self


class CrowdEstimateRequest(JuguangModel):
    advertiser_id: int = Field(gt=0)
    marketing_target: int = 4
    placement: Literal[1, 2, 4, 7]
    optimize_target: int = Field(ge=0)
    target_type: Literal[1, 2, 3]
    target_config: CrowdEstimateTargetConfig


class CrowdEstimate(JuguangModel):
    crowd_scope: Literal[1, 2, 3]
    crowd_num: str
    raw_crowd_num: int = Field(ge=0)


class KeywordRecommendation(JuguangModel):
    keyword: str
    source: Optional[int] = None
    bid: int = Field(default=0, ge=0)
    competition_level: Optional[str] = None
    recommend_reason: Tuple[str, ...] = tuple()
    monthpv: int = Field(default=0, ge=0)


class KeywordRecommendRequest(JuguangModel):
    advertiser_id: int = Field(gt=0)
    request_type: Literal["note", "industry", "search", "session"]
    promotion_target: Optional[int] = None
    recommend_reason_filter: Tuple[str, ...] = tuple()
    keyword: Optional[str] = None
    item_ids: Tuple[str, ...] = tuple()
    taxonomy_id: Optional[str] = None
    attribute_list: Optional[str] = None
    attribute_name_list: Optional[str] = None
    rank: Optional[Literal[1, 2, 3, 4]] = None

    @model_validator(mode="after")
    def validate_request_type(self) -> "KeywordRecommendRequest":
        if self.request_type == "note" and (
            self.promotion_target is None or not self.item_ids
        ):
            raise ValueError(
                "note recommendations require promotion_target and item_ids"
            )
        if self.request_type == "industry" and not self.taxonomy_id:
            raise ValueError("industry recommendations require taxonomy_id")
        if self.request_type in {"search", "session"} and not self.keyword:
            raise ValueError("search/session recommendations require keyword")
        return self


class KeywordRecommendResult(JuguangModel):
    bag_month_pv: int = Field(default=0, ge=0)
    word_num: int = Field(default=0, ge=0)
    word_list: Tuple[KeywordRecommendation, ...] = tuple()


class WordBag(JuguangModel):
    name: str
    source: Optional[int] = None
    word_list: Tuple[KeywordRecommendation, ...] = tuple()
    create_audit: Optional[str] = None
    create_time: Optional[str] = None
    keyword_source: Optional[int] = None


class WordBagPage(JuguangModel):
    page_num: int = Field(default=1, ge=1)
    total_count: int = Field(default=0, ge=0)


class WordBagResult(JuguangModel):
    page: WordBagPage
    word_tag_dto_list: Tuple[WordBag, ...] = tuple()


class TargetKeywordRecommendation(JuguangModel):
    target_word: str
    recommend_reason: Tuple[str, ...] = tuple()
    cover_num: int = Field(default=0, ge=0)


class JuguangCreateResult(JuguangModel):
    info_list: Tuple[Dict[str, Any], ...] = tuple()


class JuguangUnitList(JuguangModel):
    total_count: int = Field(default=0, ge=0)
    unit_infos: Tuple[Dict[str, Any], ...] = tuple()


class JuguangCampaignList(JuguangModel):
    page: Dict[str, Any] = Field(default_factory=dict)
    base_campaign_dtos: Tuple[Dict[str, Any], ...] = tuple()


class JuguangCreativityList(JuguangModel):
    page: Dict[str, Any] = Field(default_factory=dict)
    creativity_dtos: Tuple[Dict[str, Any], ...] = tuple()


class JuguangEnvelope(JuguangModel):
    code: int
    msg: str = ""
    success: bool
    request_id: Optional[str] = None
    data: Any = None


@dataclass(frozen=True)
class SourceProof:
    source_system: str
    query_or_endpoint: str
    request_parameters: Mapping[str, Any]
    account_scope: str
    retrieved_at: str
    raw_payload_sha256: str
    normalized_payload_sha256: str
    producer_version: str = "seeding-juguang-v1"
    raw_capture_mode: Literal["VALIDATED_DTO"] = "VALIDATED_DTO"


@dataclass(frozen=True)
class JuguangWriteGuard:
    enabled: bool = False
    environment: Literal["TEST", "PRODUCTION"] = "TEST"
    advertiser_allowlist: Tuple[int, ...] = tuple()

    def require_write(
        self,
        advertiser_id: int,
        *,
        environment: Literal["TEST", "PRODUCTION"],
    ) -> None:
        if not self.enabled:
            raise SeedingError(
                "FORMAL_EXECUTION_LOCKED",
                "Juguang platform writes are disabled",
                status_code=423,
            )
        if self.environment != environment:
            raise SeedingError(
                "FORMAL_EXECUTION_LOCKED",
                "write guard environment does not match the requested operation",
                status_code=423,
            )
        if advertiser_id not in self.advertiser_allowlist:
            raise SeedingError(
                "ACCOUNT_NOT_ALLOWLISTED",
                "advertiser is not in the test write allowlist",
                status_code=403,
            )

    def require_test_write(self, advertiser_id: int) -> None:
        self.require_write(advertiser_id, environment="TEST")


def token_provider_from_environment() -> Callable[[], str]:
    """Return a provider that reads a token from a runtime-only file.

    The token itself is never included in an exception or returned value.
    """

    token_file = os.getenv("JUGUANG_ACCESS_TOKEN_FILE", "").strip()

    def provider() -> str:
        if not token_file:
            raise SeedingError(
                "PLATFORM_CREDENTIALS_MISSING",
                "JUGUANG_ACCESS_TOKEN_FILE is not configured",
                status_code=503,
            )
        path = Path(token_file)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SeedingError(
                "PLATFORM_CREDENTIALS_UNAVAILABLE",
                "Juguang token file cannot be read",
                status_code=503,
            ) from exc
        if not token:
            raise SeedingError(
                "PLATFORM_CREDENTIALS_UNAVAILABLE",
                "Juguang token file is empty",
                status_code=503,
            )
        return token

    return provider


class JuguangClient:
    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        http_client: Optional[httpx.Client] = None,
        write_guard: Optional[JuguangWriteGuard] = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._token_provider = token_provider
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout_seconds)
        self.write_guard = write_guard or JuguangWriteGuard()

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> "JuguangClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        report_api: bool = False,
    ) -> JuguangEnvelope:
        token = self._token_provider()
        base_url = JUGUANG_REPORT_BASE_URL if report_api else JUGUANG_API_BASE_URL
        try:
            response = self._http.post(
                base_url + path,
                headers={"Access-Token": token, "Content-Type": "application/json"},
                json=dict(payload),
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SeedingError(
                "PLATFORM_TRANSPORT_ERROR",
                "Juguang request failed before a valid business response was received",
                status_code=502,
                details={"endpoint": path},
            ) from exc
        try:
            envelope = JuguangEnvelope.model_validate(body)
        except ValidationError as exc:
            raise SeedingError(
                "PLATFORM_SCHEMA_ERROR",
                "Juguang returned an invalid response envelope",
                status_code=502,
                details={"endpoint": path},
            ) from exc
        if not envelope.success or envelope.code != 0:
            safe_message = envelope.msg.replace(token, "[REDACTED]")[:300]
            raise SeedingError(
                "PLATFORM_BUSINESS_ERROR",
                "Juguang rejected the request",
                status_code=502,
                details={
                    "endpoint": path,
                    "platform_code": envelope.code,
                    "platform_message": safe_message,
                    "request_id": envelope.request_id,
                },
            )
        return envelope

    @staticmethod
    def _validate_data(model: Any, data: Any, *, endpoint: str) -> Any:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise SeedingError(
                "PLATFORM_SCHEMA_ERROR",
                "Juguang returned data that does not match the pinned contract",
                status_code=502,
                details={"endpoint": endpoint},
            ) from exc

    def get_available_target_info(
        self, *, advertiser_id: int, marketing_target: int = 4
    ) -> AvailableTargetInfo:
        envelope = self._post(
            AVAILABLE_TARGET_INFO_PATH,
            {"advertiser_id": advertiser_id, "marketing_target": marketing_target},
        )
        return self._validate_data(
            AvailableTargetInfo, envelope.data, endpoint=AVAILABLE_TARGET_INFO_PATH
        )

    def estimate_crowd(self, request: CrowdEstimateRequest) -> CrowdEstimate:
        envelope = self._post(
            CROWD_ESTIMATE_PATH,
            request.model_dump(mode="json", exclude_none=True),
        )
        return self._validate_data(
            CrowdEstimate, envelope.data, endpoint=CROWD_ESTIMATE_PATH
        )

    def recommend_keywords(
        self, request: KeywordRecommendRequest
    ) -> KeywordRecommendResult:
        envelope = self._post(
            KEYWORD_RECOMMEND_PATH,
            request.model_dump(mode="json", exclude_none=True),
        )
        return self._validate_data(
            KeywordRecommendResult, envelope.data, endpoint=KEYWORD_RECOMMEND_PATH
        )

    def list_word_bags(
        self,
        *,
        advertiser_id: int,
        name: Optional[str] = None,
        category: Optional[str] = None,
        page_num: int = 1,
        page_size: int = 5,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> WordBagResult:
        if not 1 <= page_size <= 5:
            raise ValueError("Juguang word-bag page_size must be between 1 and 5")
        payload = {
            key: value
            for key, value in {
                "advertiser_id": advertiser_id,
                "name": name,
                "category": category,
                "page_num": page_num,
                "page_size": page_size,
                "start_time": start_time,
                "end_time": end_time,
            }.items()
            if value is not None
        }
        envelope = self._post(WORD_BAG_LIST_PATH, payload)
        return self._validate_data(
            WordBagResult, envelope.data, endpoint=WORD_BAG_LIST_PATH
        )

    def recommend_target_keywords(
        self,
        *,
        advertiser_id: int,
        note_ids: Sequence[str] = (),
        keyword: Optional[str] = None,
    ) -> Tuple[TargetKeywordRecommendation, ...]:
        if not note_ids and not keyword:
            raise ValueError("note_ids or keyword is required")
        envelope = self._post(
            TARGET_KEYWORD_RECOMMEND_PATH,
            {
                "advertiser_id": advertiser_id,
                "note_ids": list(note_ids),
                "keyword": keyword,
            },
        )
        return tuple(
            self._validate_data(
                TargetKeywordRecommendation,
                item,
                endpoint=TARGET_KEYWORD_RECOMMEND_PATH,
            )
            for item in (envelope.data or [])
        )

    def list_units(
        self,
        *,
        advertiser_id: int,
        campaign_id: Optional[int] = None,
        unit_ids: Sequence[int] = (),
        unit_name: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> JuguangUnitList:
        if len(unit_ids) > 10:
            raise ValueError("Juguang unit_ids supports at most 10 values")
        payload = {
            key: value
            for key, value in {
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "unit_ids": list(unit_ids),
                "unit_name": unit_name,
                "page": page,
                "page_size": page_size,
            }.items()
            if value is not None and value != []
        }
        envelope = self._post(UNIT_LIST_PATH, payload)
        return self._validate_data(
            JuguangUnitList, envelope.data, endpoint=UNIT_LIST_PATH
        )

    def list_campaigns(
        self,
        *,
        advertiser_id: int,
        campaign_ids: Sequence[int] = (),
        campaign_name: Optional[str] = None,
        status: Optional[int] = None,
        page_index: int = 1,
        page_size: int = 20,
    ) -> JuguangCampaignList:
        if len(campaign_ids) > 20:
            raise ValueError("Juguang campaign_ids supports at most 20 values")
        payload = {
            key: value
            for key, value in {
                "advertiser_id": advertiser_id,
                "campaign_ids": list(campaign_ids),
                "campaign_name": campaign_name,
                "status": status,
                "page": {"page_index": page_index, "page_size": page_size},
            }.items()
            if value is not None and value != []
        }
        envelope = self._post(CAMPAIGN_LIST_PATH, payload)
        return self._validate_data(
            JuguangCampaignList, envelope.data, endpoint=CAMPAIGN_LIST_PATH
        )

    def search_creativities(
        self,
        *,
        advertiser_id: int,
        campaign_id: Optional[int] = None,
        unit_id: Optional[int] = None,
        creativity_ids: Sequence[int] = (),
        note_id: Optional[str] = None,
        page_index: int = 1,
        page_size: int = 20,
    ) -> JuguangCreativityList:
        if len(creativity_ids) > 20:
            raise ValueError("Juguang creativity_ids supports at most 20 values")
        payload = {
            key: value
            for key, value in {
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "unit_id": unit_id,
                "creativity_ids": list(creativity_ids),
                "note_id": note_id,
                "page": {"page_index": page_index, "page_size": page_size},
            }.items()
            if value is not None and value != []
        }
        envelope = self._post(CREATIVITY_SEARCH_PATH, payload)
        return self._validate_data(
            JuguangCreativityList, envelope.data, endpoint=CREATIVITY_SEARCH_PATH
        )

    def group_report_v2(self, payload: Mapping[str, Any]) -> Any:
        return self._post(GROUP_REPORT_V2_PATH, payload, report_api=True).data

    def create_cascade(
        self,
        *,
        advertiser_id: int,
        payload: Mapping[str, Any],
        environment: Literal["TEST", "PRODUCTION"] = "TEST",
    ) -> JuguangCreateResult:
        self.write_guard.require_write(advertiser_id, environment=environment)
        body = dict(payload)
        if body.get("advertiser_id") != advertiser_id:
            raise SeedingError(
                "ACCOUNT_SCOPE_MISMATCH",
                "payload advertiser_id does not match authorized advertiser",
                status_code=409,
            )
        envelope = self._post(CASCADE_CREATE_PATH, body)
        return self._validate_data(
            JuguangCreateResult, envelope.data, endpoint=CASCADE_CREATE_PATH
        )

    def update_campaign_status(
        self,
        *,
        advertiser_id: int,
        campaign_ids: Sequence[int],
        action_type: Literal[1, 2, 3],
        environment: Literal["TEST", "PRODUCTION"] = "TEST",
    ) -> Tuple[int, ...]:
        self.write_guard.require_write(advertiser_id, environment=environment)
        if not 1 <= len(campaign_ids) <= 20:
            raise ValueError("campaign status update requires 1-20 campaign ids")
        envelope = self._post(
            CAMPAIGN_STATUS_UPDATE_PATH,
            {
                "advertiser_id": advertiser_id,
                "campaign_ids": list(campaign_ids),
                "action_type": action_type,
            },
        )
        if not isinstance(envelope.data, Mapping):
            raise SeedingError(
                "PLATFORM_SCHEMA_ERROR",
                "Juguang campaign status response is not an object",
                status_code=502,
                details={"endpoint": CAMPAIGN_STATUS_UPDATE_PATH},
            )
        try:
            return tuple(int(value) for value in envelope.data.get("campaign_ids", []))
        except (TypeError, ValueError) as exc:
            raise SeedingError(
                "PLATFORM_SCHEMA_ERROR",
                "Juguang campaign status response contains invalid ids",
                status_code=502,
                details={"endpoint": CAMPAIGN_STATUS_UPDATE_PATH},
            ) from exc


def make_source_proof(
    *,
    endpoint: str,
    request_parameters: Mapping[str, Any],
    account_scope: str,
    retrieved_at: str,
    raw_payload: Any,
    normalized_payload: Any,
) -> SourceProof:
    return SourceProof(
        source_system="XIAOHONGSHU_JUGUANG",
        query_or_endpoint=endpoint,
        request_parameters=dict(request_parameters),
        account_scope=account_scope,
        retrieved_at=retrieved_at,
        raw_payload_sha256=sha256_json(raw_payload),
        normalized_payload_sha256=sha256_json(normalized_payload),
    )
