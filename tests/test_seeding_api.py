from __future__ import annotations

from fastapi.testclient import TestClient
from seeding_fixtures import prepare_request, project_config

from app.seeding.api import create_seeding_app
from app.seeding.auth import StaticAuthenticator

HEADERS = {
    "X-CID-Operator-Id": "owner-1",
    "X-CID-Operator-Token": "test-token-not-a-secret",
    "X-CID-Session-Id": "test-session",
}


def test_api_is_authenticated_async_and_execution_locked(tmp_path) -> None:
    auth = StaticAuthenticator(
        {
            "owner-1": {
                "token": "test-token-not-a-secret",
                "roles": ["viewer", "operator", "approver", "executor", "admin"],
            }
        }
    )
    app = create_seeding_app(
        database_path=tmp_path / "seeding.sqlite3",
        authenticator=auth,
        start_worker=False,
    )
    config = project_config()
    request = prepare_request(config)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["platform_adapter_mounted"] is False
        assert health.json()["platform_business_write_count"] == 0

        unauthenticated = client.get(
            f"/api/seeding/v1/projects/{config.project_id}/preview"
        )
        assert unauthenticated.status_code == 401

        created = client.post(
            "/api/seeding/v1/projects",
            headers=HEADERS,
            json=config.model_dump(mode="json", by_alias=True),
        )
        assert created.status_code == 201

        prepared = client.post(
            f"/api/seeding/v1/projects/{config.project_id}/prepare",
            headers=HEADERS,
            json=request.model_dump(mode="json", by_alias=True),
        )
        assert prepared.status_code == 202
        assert prepared.json()["status"] == "PENDING"
        app.state.seeding_service.process_next_job(worker_id="api-test-worker")

        preview = client.get(
            f"/api/seeding/v1/projects/{config.project_id}/preview",
            headers=HEADERS,
        )
        assert preview.status_code == 200
        assert preview.json()["project"]["project_state"] == "WAITING_CONFIRMATION"
        assert preview.json()["platform_business_write_count"] == 0

        for suffix in ("authorize-test-write", "execute", "release-grants"):
            locked = client.post(
                f"/api/seeding/v1/projects/{config.project_id}/{suffix}",
                headers=HEADERS,
            )
            assert locked.status_code == 423
            assert locked.json()["detail"]["code"] == "FORMAL_EXECUTION_LOCKED"
            assert (
                locked.json()["detail"]["details"]["platform_business_write_count"] == 0
            )
