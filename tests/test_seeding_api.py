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
        assert client.get("/ready").json()["ready_for_planning"] is True
        console = client.get("/console")
        assert console.status_code == 200
        assert "SEEDING Control Console" in console.text
        assert "Send authenticated request" in console.text
        assert console.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in console.headers["content-security-policy"]

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

        # Test execution now has typed request contracts; malformed empty bodies
        # are rejected before any business authorization or platform call.
        for suffix in ("authorize-test-write", "execute"):
            rejected = client.post(
                f"/api/seeding/v1/projects/{config.project_id}/{suffix}",
                headers=HEADERS,
            )
            assert rejected.status_code == 422

        rejected = client.post(
            f"/api/seeding/v1/projects/{config.project_id}/release-grants",
            headers=HEADERS,
        )
        assert rejected.status_code == 422


def test_readiness_is_unavailable_without_authentication_configuration(
    tmp_path,
) -> None:
    app = create_seeding_app(
        database_path=tmp_path / "seeding.sqlite3",
        start_worker=False,
    )
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["ready_for_planning"] is False
