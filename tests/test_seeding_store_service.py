from __future__ import annotations

import pytest
from seeding_fixtures import NOW, prepare_request, project_config

from app.seeding.contracts import ConfirmRequest
from app.seeding.errors import ExecutionLocked, SeedingError
from app.seeding.identity import sha256_json
from app.seeding.service import SeedingService
from app.seeding.store import SeedingStore


def test_prepare_job_builds_feed_matrix_without_platform_writes(tmp_path) -> None:
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    service = SeedingService(store)
    config = project_config()
    service.create_project(config, principal_id="owner-1")
    request = prepare_request(config)

    job = service.enqueue_prepare(config.project_id, request, principal_id="owner-1")
    assert job["status"] == "PENDING"
    result = service.process_next_job(worker_id="test-worker")

    assert result is not None
    assert result["content"]["status"] == "WAITING_CONFIRMATION"
    assert len(result["content"]["plans"]) == 4
    assert result["content"]["platform_business_write_count"] == 0
    preview = service.preview(config.project_id, principal_id="owner-1")
    assert preview["project"]["project_state"] == "WAITING_CONFIRMATION"
    assert len(preview["active_plans"]) == 4
    assert preview["platform_business_write_count"] == 0


def test_prepare_is_idempotent_for_same_input(tmp_path) -> None:
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    service = SeedingService(store)
    config = project_config()
    service.create_project(config, principal_id="owner-1")
    request = prepare_request(config)
    first = service.enqueue_prepare(config.project_id, request, principal_id="owner-1")
    second = service.enqueue_prepare(config.project_id, request, principal_id="owner-1")
    assert first["job_id"] == second["job_id"]
    service.process_next_job(worker_id="test-worker")
    replay = service.enqueue_prepare(config.project_id, request, principal_id="owner-1")
    assert replay["job_id"] == first["job_id"]
    assert replay["status"] == "SUCCEEDED"


def test_expired_running_lease_is_reclaimed(tmp_path) -> None:
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    service = SeedingService(store)
    config = project_config()
    service.create_project(config, principal_id="owner-1")
    request = prepare_request(config)
    job = service.enqueue_prepare(config.project_id, request, principal_id="owner-1")
    claimed = store.claim_job(worker_id="crashed-worker", lease_seconds=60)
    assert claimed is not None
    with store.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job["job_id"],),
        )
    reclaimed = store.claim_job(worker_id="replacement-worker", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed["job_id"] == job["job_id"]
    assert reclaimed["lease_owner"] == "replacement-worker"
    assert reclaimed["attempt"] == 2


def test_insufficient_budget_enters_waiting_budget_with_no_plans(tmp_path) -> None:
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    service = SeedingService(store)
    config = project_config()
    service.create_project(config, principal_id="owner-1")
    request = prepare_request(config, phase_budget_fen=50_000)
    service.enqueue_prepare(config.project_id, request, principal_id="owner-1")
    result = service.process_next_job(worker_id="test-worker")
    assert result is not None
    assert result["content"]["status"] == "WAITING_BUDGET"
    assert result["content"]["plans"] == []
    assert store.get_project(config.project_id)["project_state"] == "WAITING_BUDGET"


def test_confirmation_uses_config_and_matrix_compare_and_set(tmp_path) -> None:
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    service = SeedingService(store)
    config = project_config()
    service.create_project(config, principal_id="owner-1")
    service.enqueue_prepare(
        config.project_id, prepare_request(config), principal_id="owner-1"
    )
    service.process_next_job(worker_id="test-worker")
    matrix = store.latest_artifact(config.project_id, "plan_matrix")
    object_hashes = tuple(
        sorted(
            sha256_json(item["payload"])
            for item in store.list_active_plans(config.project_id)
        )
    )
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
    assert confirmation["kind"] == "confirmation"
    assert store.get_project(config.project_id)["project_state"] == "ADVISORY_READY"


def test_wrong_matrix_sha_is_rejected(tmp_path) -> None:
    store = SeedingStore(tmp_path / "seeding.sqlite3")
    service = SeedingService(store)
    config = project_config()
    service.create_project(config, principal_id="owner-1")
    service.enqueue_prepare(
        config.project_id, prepare_request(config), principal_id="owner-1"
    )
    service.process_next_job(worker_id="test-worker")
    with pytest.raises(SeedingError) as exc:
        service.confirm(
            config.project_id,
            ConfirmRequest(
                config_sha=sha256_json(config),
                matrix_sha="0" * 64,
                object_hashes=("0" * 64,),
                confirmed_by="owner-1",
                confirmed_at=NOW,
            ),
            principal_id="owner-1",
        )
    assert exc.value.code == "MATRIX_SHA_MISMATCH"


def test_non_owner_cannot_read_project(tmp_path) -> None:
    service = SeedingService(SeedingStore(tmp_path / "seeding.sqlite3"))
    config = project_config()
    service.create_project(config, principal_id="owner-1")
    with pytest.raises(SeedingError) as exc:
        service.preview(config.project_id, principal_id="intruder")
    assert exc.value.code == "RBAC_FORBIDDEN"


def test_platform_execution_remains_locked() -> None:
    with pytest.raises(ExecutionLocked) as exc:
        SeedingService.locked_execution()
    assert exc.value.status_code == 423
    assert exc.value.details["platform_business_write_count"] == 0
