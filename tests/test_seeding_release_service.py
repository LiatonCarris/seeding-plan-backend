from __future__ import annotations

from datetime import datetime, timedelta, timezone

from seeding_fixtures import NOW, SHA, compiled_prepare_request, project_config

from app.seeding.contracts import (
    ConfirmRequest,
    ExecuteRequest,
    IssuePausedCreateGrantRequest,
    IssueReleaseGrantRequest,
    PausedCreateGrant,
    ProjectConfig,
    ReleaseEvaluationRequest,
    ReleaseExecuteRequest,
    ReleaseGrant,
    ReleaseObjectBinding,
)
from app.seeding.execution import (
    BatchExecutionReceipt,
    ObjectIds,
    PlanReadbackReceipt,
)
from app.seeding.grants import GrantSigner
from app.seeding.identity import sha256_json
from app.seeding.release import ReleaseReceipt, SpendEvaluationReceipt
from app.seeding.service import SeedingService
from app.seeding.store import SeedingStore


class ProductionCreateEngine:
    def execute(self, *, advertiser_id: int, plans):
        return BatchExecutionReceipt(
            status="VERIFIED_PAUSED",
            advertiser_id=advertiser_id,
            plan_receipts=tuple(
                PlanReadbackReceipt(
                    plan_revision_id=plan.plan_revision_id,
                    logical_plan_key=plan.logical_plan_key,
                    platform_request_sha256=plan.payload_sha256,
                    object_ids=ObjectIds(
                        campaign_id=1000 + index,
                        unit_id=2000 + index,
                        creativity_ids=(3000 + index,),
                    ),
                    expected_sha256=plan.payload_sha256,
                    actual_sha256="f" * 64,
                    pause_verified=True,
                    readback_verified=True,
                    diff=tuple(),
                )
                for index, plan in enumerate(plans, start=1)
            ),
            relock_receipt_id="production-relock-1",
            lock_verified=True,
        )


class ReleaseEngine:
    def __init__(self) -> None:
        self.spend_fen = 0

    def activate(
        self,
        *,
        advertiser_id: int,
        campaign_ids,
        spend_cap_fen: int,
        spend_cap_period: str,
        monitor_source: str,
    ) -> ReleaseReceipt:
        return ReleaseReceipt(
            status="RELEASE_ACTIVE",
            advertiser_id=advertiser_id,
            campaign_ids=tuple(campaign_ids),
            spend_cap_fen=spend_cap_fen,
            spend_cap_period=spend_cap_period,
            safety_receipt_id="cap-armed-1",
            platform_enabled_ids=tuple(campaign_ids),
            readback_enabled=True,
        )

    def evaluate_spend(
        self,
        *,
        advertiser_id: int,
        campaign_ids,
        spend_cap_fen: int,
        spend_cap_period: str,
    ) -> SpendEvaluationReceipt:
        return SpendEvaluationReceipt(
            status=(
                "CAP_EXCEEDED_PAUSED"
                if self.spend_fen > spend_cap_fen
                else "WITHIN_CAP"
            ),
            advertiser_id=advertiser_id,
            campaign_ids=tuple(campaign_ids),
            observed_spend_fen=self.spend_fen,
            spend_cap_fen=spend_cap_fen,
            emergency_relock_receipt_id=(
                "release-relock-1" if self.spend_fen > spend_cap_fen else None
            ),
        )

    def pause_and_relock(self, *, advertiser_id: int, campaign_ids):
        return "release-recovery-lock-1", True


def production_config() -> ProjectConfig:
    payload = project_config().model_dump(mode="json", by_alias=True)
    payload["advertiser"].update(
        {
            "environment": "PRODUCTION",
            "account_id": "production-account",
            "advertiser_id": 999,
            "advertiser_account_name": "Production advertiser",
        }
    )
    return ProjectConfig.model_validate(payload)


def confirmed_production_service(tmp_path):
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    release_engine = ReleaseEngine()
    service = SeedingService(
        store,
        production_create_engine=ProductionCreateEngine(),
        release_engine=release_engine,
        grant_signer=GrantSigner(b"r" * 32),
    )
    config = production_config()
    service.create_project(config, principal_id="owner-1")
    service.enqueue_prepare(
        config.project_id,
        compiled_prepare_request(config),
        principal_id="owner-1",
    )
    service.process_next_job(worker_id="prepare-worker")
    matrix = store.latest_artifact(config.project_id, "plan_matrix")
    plans = sorted(
        store.list_active_plans(config.project_id),
        key=lambda item: item["plan_revision_id"],
    )
    object_hashes = tuple(sorted(sha256_json(item["payload"]) for item in plans))
    confirmation = service.confirm(
        config.project_id,
        ConfirmRequest(
            config_sha=sha256_json(config),
            matrix_sha=matrix["content_sha"],
            object_hashes=object_hashes,
            confirmed_by="owner-1",
            confirmed_at=NOW,
        ),
        principal_id="owner-1",
    )
    return (
        service,
        store,
        release_engine,
        config,
        matrix,
        plans,
        object_hashes,
        confirmation,
    )


def test_production_paused_create_and_independent_release_grants(tmp_path) -> None:
    (
        service,
        store,
        release_engine,
        config,
        matrix,
        plans,
        object_hashes,
        confirmation,
    ) = confirmed_production_service(tmp_path)
    platform_hashes = tuple(
        item["payload"]["platform_payload_sha256"] for item in plans
    )
    paused_row = service.issue_paused_create_grant(
        config.project_id,
        IssuePausedCreateGrantRequest(
            config_sha=sha256_json(config),
            matrix_sha=matrix["content_sha"],
            confirmation_sha=confirmation["content_sha"],
            test_readback_receipt_sha=SHA,
            object_hashes=object_hashes,
            plan_revision_ids=tuple(item["plan_revision_id"] for item in plans),
            platform_payload_hashes=platform_hashes,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            nonce="production-create-nonce-1",
        ),
        principal_id="owner-1",
        principal_role="admin",
    )
    paused_grant = PausedCreateGrant.model_validate(paused_row["payload"])
    service.enqueue_production_create(
        config.project_id,
        ExecuteRequest(
            grant_id=paused_grant.grant_id,
            grant_sha256=paused_grant.grant_sha256,
            signature_sha256=paused_grant.signature_sha256,
        ),
        principal_id="owner-1",
    )
    create_result = service.process_next_job(worker_id="production-create-worker")
    assert create_result is not None
    assert create_result["status"] == "VERIFIED_PAUSED"
    assert (
        store.get_project(config.project_id)["execution_state"]
        == "WAITING_RELEASE_AUTHORIZATION"
    )

    readback = store.latest_artifact(config.project_id, "production_paused_readback")
    campaign_ids = tuple(
        item["object_ids"]["campaign_id"]
        for item in readback["content"]["plan_receipts"]
    )
    bindings = tuple(
        ReleaseObjectBinding(
            plan_revision_id=item["plan_revision_id"],
            campaign_id=item["object_ids"]["campaign_id"],
            unit_id=item["object_ids"]["unit_id"],
            creativity_ids=tuple(item["object_ids"]["creativity_ids"]),
        )
        for item in readback["content"]["plan_receipts"]
    )
    release_row = service.issue_release_grant(
        config.project_id,
        IssueReleaseGrantRequest(
            plan_revision_ids=tuple(item["plan_revision_id"] for item in plans),
            platform_campaign_ids=campaign_ids,
            platform_object_bindings=bindings,
            config_sha=sha256_json(config),
            matrix_sha=matrix["content_sha"],
            confirmation_sha=confirmation["content_sha"],
            payload_sha=sha256_json(platform_hashes),
            readback_receipt_sha=readback["content_sha"],
            start_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            spend_cap_fen=50_000,
            spend_cap_period="TOTAL",
            spend_monitor_source="juguang-group-report-v2",
            spend_monitor_proof_sha256=SHA,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            nonce="release-enable-nonce-1",
        ),
        principal_id="owner-1",
        principal_role="admin",
    )
    release_grant = ReleaseGrant.model_validate(release_row["payload"])
    service.enqueue_release(
        config.project_id,
        release_grant.release_grant_id,
        ReleaseExecuteRequest(
            grant_sha256=release_grant.grant_sha256,
            signature_sha256=release_grant.signature_sha256,
        ),
        principal_id="owner-1",
    )
    release_result = service.process_next_job(worker_id="release-worker")
    assert release_result is not None
    assert release_result["status"] == "RELEASE_ACTIVE"
    assert store.get_project(config.project_id)["delivery_state"] == "FEED_LEARNING"

    release_engine.spend_fen = 50_001
    service.enqueue_release_evaluation(
        config.project_id,
        release_grant.release_grant_id,
        ReleaseEvaluationRequest(
            evaluation_key="2026-09-17-total",
            grant_sha256=release_grant.grant_sha256,
            signature_sha256=release_grant.signature_sha256,
        ),
        principal_id="owner-1",
    )
    evaluation = service.process_next_job(worker_id="evaluation-worker")
    assert evaluation is not None
    assert evaluation["status"] == "CAP_EXCEEDED_PAUSED"
    assert store.get_project(config.project_id)["delivery_state"] == "CLOSED"


def test_release_worker_crash_requires_explicit_emergency_recovery(tmp_path) -> None:
    (
        service,
        store,
        _,
        config,
        matrix,
        plans,
        object_hashes,
        confirmation,
    ) = confirmed_production_service(tmp_path)
    platform_hashes = tuple(
        item["payload"]["platform_payload_sha256"] for item in plans
    )
    paused_row = service.issue_paused_create_grant(
        config.project_id,
        IssuePausedCreateGrantRequest(
            config_sha=sha256_json(config),
            matrix_sha=matrix["content_sha"],
            confirmation_sha=confirmation["content_sha"],
            test_readback_receipt_sha=SHA,
            object_hashes=object_hashes,
            plan_revision_ids=tuple(item["plan_revision_id"] for item in plans),
            platform_payload_hashes=platform_hashes,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            nonce="production-create-nonce-2",
        ),
        principal_id="owner-1",
        principal_role="admin",
    )
    paused_grant = PausedCreateGrant.model_validate(paused_row["payload"])
    service.enqueue_production_create(
        config.project_id,
        ExecuteRequest(
            grant_id=paused_grant.grant_id,
            grant_sha256=paused_grant.grant_sha256,
            signature_sha256=paused_grant.signature_sha256,
        ),
        principal_id="owner-1",
    )
    service.process_next_job(worker_id="production-create-worker")
    readback = store.latest_artifact(config.project_id, "production_paused_readback")
    bindings = tuple(
        ReleaseObjectBinding(
            plan_revision_id=item["plan_revision_id"],
            campaign_id=item["object_ids"]["campaign_id"],
            unit_id=item["object_ids"]["unit_id"],
            creativity_ids=tuple(item["object_ids"]["creativity_ids"]),
        )
        for item in readback["content"]["plan_receipts"]
    )
    release_row = service.issue_release_grant(
        config.project_id,
        IssueReleaseGrantRequest(
            plan_revision_ids=tuple(item.plan_revision_id for item in bindings),
            platform_campaign_ids=tuple(item.campaign_id for item in bindings),
            platform_object_bindings=bindings,
            config_sha=sha256_json(config),
            matrix_sha=matrix["content_sha"],
            confirmation_sha=confirmation["content_sha"],
            payload_sha=sha256_json(platform_hashes),
            readback_receipt_sha=readback["content_sha"],
            start_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            spend_cap_fen=50_000,
            spend_cap_period="TOTAL",
            spend_monitor_source="juguang-group-report-v2",
            spend_monitor_proof_sha256=SHA,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            nonce="release-enable-nonce-2",
        ),
        principal_id="owner-1",
        principal_role="admin",
    )
    grant = ReleaseGrant.model_validate(release_row["payload"])
    release_job = service.enqueue_release(
        config.project_id,
        grant.release_grant_id,
        ReleaseExecuteRequest(
            grant_sha256=grant.grant_sha256,
            signature_sha256=grant.signature_sha256,
        ),
        principal_id="owner-1",
    )
    store.claim_job(worker_id="crashed-release-worker")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (release_job["job_id"],),
        )
    assert store.claim_job(worker_id="replacement-worker") is None
    assert store.get_job(release_job["job_id"])["status"] == "RECONCILE_REQUIRED"
    recovery = service.enqueue_reconcile(
        config.project_id,
        release_job["job_id"],
        principal_id="owner-1",
    )
    result = service.process_next_job(worker_id="release-recovery-worker")
    assert result is not None
    assert result["status"] == "RELEASE_RECOVERED_SAFE"
    assert store.get_job(recovery["job_id"])["status"] == "SUCCEEDED"
    assert store.get_project(config.project_id)["delivery_state"] == "CLOSED"
