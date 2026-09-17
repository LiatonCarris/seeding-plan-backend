from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from seeding_fixtures import NOW, compiled_prepare_request, project_config

from app.seeding.contracts import (
    ConfirmRequest,
    ExecuteRequest,
    ExecutionGrant,
    IssueExecutionGrantRequest,
)
from app.seeding.errors import SeedingError
from app.seeding.execution import BatchExecutionReceipt, PlanReadbackReceipt
from app.seeding.grants import GrantSigner
from app.seeding.identity import sha256_json
from app.seeding.service import SeedingService
from app.seeding.store import SeedingStore


class SuccessfulExecutionEngine:
    def execute(self, *, advertiser_id: int, plans):
        return BatchExecutionReceipt(
            status="VERIFIED_PAUSED",
            advertiser_id=advertiser_id,
            plan_receipts=tuple(
                PlanReadbackReceipt(
                    plan_revision_id=plan.plan_revision_id,
                    logical_plan_key=plan.logical_plan_key,
                    platform_request_sha256=plan.payload_sha256,
                    object_ids=None,
                    expected_sha256=plan.payload_sha256,
                    actual_sha256="f" * 64,
                    pause_verified=True,
                    readback_verified=True,
                    diff=tuple(),
                )
                for plan in plans
            ),
            relock_receipt_id="lock-receipt-1",
            lock_verified=True,
        )


class IndeterminateThenSafeEngine(SuccessfulExecutionEngine):
    def execute(self, *, advertiser_id: int, plans):
        result = super().execute(advertiser_id=advertiser_id, plans=plans)
        return BatchExecutionReceipt(
            status="RECONCILE_REQUIRED",
            advertiser_id=advertiser_id,
            plan_receipts=result.plan_receipts,
            relock_receipt_id=result.relock_receipt_id,
            lock_verified=True,
        )

    def reconcile(self, *, advertiser_id: int, plans):
        result = super().execute(advertiser_id=advertiser_id, plans=plans)
        return BatchExecutionReceipt(
            status="RECONCILED_NOT_CREATED",
            advertiser_id=advertiser_id,
            plan_receipts=result.plan_receipts,
            relock_receipt_id=result.relock_receipt_id,
            lock_verified=True,
        )


class CrashingExecutionEngine(SuccessfulExecutionEngine):
    def execute(self, *, advertiser_id: int, plans):
        raise RuntimeError("simulated in-process adapter crash")


def confirmed_service(tmp_path):
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    service = SeedingService(
        store,
        execution_engine=SuccessfulExecutionEngine(),
        grant_signer=GrantSigner(b"g" * 32),
    )
    config = project_config()
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
    grant_request = IssueExecutionGrantRequest(
        config_sha=sha256_json(config),
        matrix_sha=matrix["content_sha"],
        confirmation_sha=confirmation["content_sha"],
        object_hashes=object_hashes,
        plan_revision_ids=tuple(item["plan_revision_id"] for item in plans),
        platform_payload_hashes=tuple(
            item["payload"]["platform_payload_sha256"] for item in plans
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        nonce="unique-approval-nonce-0001",
    )
    return service, store, config, grant_request


def test_execution_grant_is_signed_consumed_once_and_executes_durably(tmp_path) -> None:
    service, store, config, request = confirmed_service(tmp_path)
    stored = service.issue_execution_grant(
        config.project_id,
        request,
        principal_id="owner-1",
        principal_role="admin",
    )
    grant = ExecutionGrant.model_validate(stored["payload"])
    execute_request = ExecuteRequest(
        grant_id=grant.grant_id,
        grant_sha256=grant.grant_sha256,
        signature_sha256=grant.signature_sha256,
    )
    job = service.enqueue_test_execution(
        config.project_id, execute_request, principal_id="owner-1"
    )
    replay = service.enqueue_test_execution(
        config.project_id, execute_request, principal_id="owner-1"
    )
    assert replay["job_id"] == job["job_id"]
    assert store.get_grant(grant.grant_id)["status"] == "CONSUMED"

    result = service.process_next_job(worker_id="execution-worker")
    assert result is not None
    assert result["status"] == "VERIFIED_PAUSED"
    completed = store.get_job(job["job_id"])
    assert completed["status"] == "SUCCEEDED"
    assert completed["result"]["lock_verified"] is True
    assert (
        store.get_project(config.project_id)["execution_state"] == "READBACK_VERIFIED"
    )
    assert len(store.list_execution_attempts(config.project_id)) == len(
        request.plan_revision_ids
    )


def test_execution_grant_signature_tamper_is_rejected(tmp_path) -> None:
    service, _, config, request = confirmed_service(tmp_path)
    stored = service.issue_execution_grant(
        config.project_id,
        request,
        principal_id="owner-1",
        principal_role="admin",
    )
    grant = ExecutionGrant.model_validate(stored["payload"])
    with pytest.raises(SeedingError) as exc:
        service.enqueue_test_execution(
            config.project_id,
            ExecuteRequest(
                grant_id=grant.grant_id,
                grant_sha256=grant.grant_sha256,
                signature_sha256="0" * 64,
            ),
            principal_id="owner-1",
        )
    assert exc.value.code == "GRANT_INTEGRITY_MISMATCH"


def test_issued_execution_grant_can_be_revoked(tmp_path) -> None:
    service, store, config, request = confirmed_service(tmp_path)
    stored = service.issue_execution_grant(
        config.project_id,
        request,
        principal_id="owner-1",
        principal_role="admin",
    )
    result = service.revoke_grant(
        config.project_id,
        stored["grant_id"],
        principal_id="owner-1",
        reason="operator cancelled the test",
    )
    assert result["status"] == "REVOKED"
    assert store.get_project(config.project_id)["execution_state"] == "NONE"


def test_expired_external_write_lease_requires_reconcile_not_replay(tmp_path) -> None:
    service, store, config, request = confirmed_service(tmp_path)
    stored = service.issue_execution_grant(
        config.project_id,
        request,
        principal_id="owner-1",
        principal_role="admin",
    )
    grant = ExecutionGrant.model_validate(stored["payload"])
    job = service.enqueue_test_execution(
        config.project_id,
        ExecuteRequest(
            grant_id=grant.grant_id,
            grant_sha256=grant.grant_sha256,
            signature_sha256=grant.signature_sha256,
        ),
        principal_id="owner-1",
    )
    claimed = store.claim_job(worker_id="crashed-worker")
    assert claimed is not None
    with store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job["job_id"],),
        )
    assert store.claim_job(worker_id="replacement-worker") is None
    assert store.get_job(job["job_id"])["status"] == "RECONCILE_REQUIRED"
    assert (
        store.get_project(config.project_id)["execution_state"] == "RECONCILE_REQUIRED"
    )


def test_external_write_worker_exception_requires_reconcile_not_retry(tmp_path) -> None:
    service, store, config, request = confirmed_service(tmp_path)
    service.execution_engine = CrashingExecutionEngine()
    stored = service.issue_execution_grant(
        config.project_id,
        request,
        principal_id="owner-1",
        principal_role="admin",
    )
    grant = ExecutionGrant.model_validate(stored["payload"])
    job = service.enqueue_test_execution(
        config.project_id,
        ExecuteRequest(
            grant_id=grant.grant_id,
            grant_sha256=grant.grant_sha256,
            signature_sha256=grant.signature_sha256,
        ),
        principal_id="owner-1",
    )
    with pytest.raises(RuntimeError):
        service.process_next_job(worker_id="crashing-worker")
    stored_job = store.get_job(job["job_id"])
    assert stored_job["status"] == "RECONCILE_REQUIRED"
    assert stored_job["attempt"] == 1
    assert (
        store.get_project(config.project_id)["execution_state"] == "RECONCILE_REQUIRED"
    )


def test_explicit_reconcile_job_never_reissues_create(tmp_path) -> None:
    service, store, config, request = confirmed_service(tmp_path)
    service.execution_engine = IndeterminateThenSafeEngine()
    stored = service.issue_execution_grant(
        config.project_id,
        request,
        principal_id="owner-1",
        principal_role="admin",
    )
    grant = ExecutionGrant.model_validate(stored["payload"])
    execution_job = service.enqueue_test_execution(
        config.project_id,
        ExecuteRequest(
            grant_id=grant.grant_id,
            grant_sha256=grant.grant_sha256,
            signature_sha256=grant.signature_sha256,
        ),
        principal_id="owner-1",
    )
    service.process_next_job(worker_id="execution-worker")
    assert store.get_job(execution_job["job_id"])["status"] == "RECONCILE_REQUIRED"
    assert (
        store.get_project(config.project_id)["execution_state"] == "RECONCILE_REQUIRED"
    )

    reconcile_job = service.enqueue_reconcile(
        config.project_id,
        execution_job["job_id"],
        principal_id="owner-1",
    )
    result = service.process_next_job(worker_id="reconcile-worker")
    assert result is not None
    assert result["status"] == "RECONCILED_NOT_CREATED"
    assert store.get_job(reconcile_job["job_id"])["status"] == "SUCCEEDED"
    assert (
        store.get_project(config.project_id)["execution_state"] == "RECOVERY_REQUIRED"
    )
