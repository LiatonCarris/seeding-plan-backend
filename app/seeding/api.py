"""FastAPI surface for the read-only SEEDING Phase 0/1 implementation."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import DenyAllAuthenticator, Principal, StaticAuthenticator
from .contracts import ConfirmRequest, PrepareRequest, ProjectConfig
from .errors import SeedingError
from .service import SeedingService
from .store import SeedingStore
from .worker import DurableWorker


def authenticator_from_environment() -> StaticAuthenticator:
    raw = os.getenv("SEEDING_OPERATOR_AUTH_JSON", "")
    if not raw:
        return DenyAllAuthenticator()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("SEEDING_OPERATOR_AUTH_JSON must be a JSON object")
    return StaticAuthenticator(payload)


def create_seeding_app(
    *,
    database_path: Path,
    authenticator: Optional[StaticAuthenticator] = None,
    start_worker: bool = True,
) -> FastAPI:
    store = SeedingStore(database_path)
    service = SeedingService(store)
    auth = authenticator or DenyAllAuthenticator()
    worker = DurableWorker(service, worker_id="api-" + uuid.uuid4().hex[:12])

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(
        title="SEEDING Read-only Planning API",
        version="0.1.0-phase0",
        description=(
            "SEEDING Phase 0/1: local contracts, deterministic planning, durable jobs, "
            "and previews. No platform adapter is mounted."
        ),
        lifespan=lifespan,
    )
    app.state.seeding_store = store
    app.state.seeding_service = service
    app.state.seeding_worker = worker

    @app.exception_handler(SeedingError)
    async def handle_seeding_error(_: Request, exc: SeedingError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.as_dict()}
        )

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "service": "seeding",
            "version": "0.1.0-phase0",
            "strategy_mode": "SEEDING",
            "platform_adapter_mounted": False,
            "formal_execution_locked": True,
            "platform_business_write_count": 0,
        }

    @app.post("/api/seeding/v1/projects", status_code=201)
    def create_project(
        config: ProjectConfig,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("operator", "admin")
        if (
            config.created_by != principal.principal_id
            or config.updated_by != principal.principal_id
        ):
            raise SeedingError(
                "ACTOR_MISMATCH",
                "created_by and updated_by must match the authenticated principal",
                status_code=403,
            )
        return service.create_project(config, principal_id=principal.principal_id)

    @app.post("/api/seeding/v1/projects/{project_id}/prepare", status_code=202)
    def prepare_project(
        project_id: str,
        request: PrepareRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("operator", "admin")
        job = service.enqueue_prepare(
            project_id, request, principal_id=principal.principal_id
        )
        return {
            "job_id": job["job_id"],
            "operation_id": job["job_id"],
            "status": job["status"],
            "status_url": f"/api/seeding/v1/projects/{project_id}/jobs/{job['job_id']}",
        }

    @app.get("/api/seeding/v1/projects/{project_id}/preview")
    def preview_project(
        project_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "admin")
        return service.preview(project_id, principal_id=principal.principal_id)

    @app.post("/api/seeding/v1/projects/{project_id}/confirm", status_code=201)
    def confirm_project(
        project_id: str,
        request: ConfirmRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("approver", "admin")
        if request.confirmed_by != principal.principal_id:
            raise SeedingError(
                "ACTOR_MISMATCH", "confirmed_by must match principal", status_code=403
            )
        return service.confirm(project_id, request, principal_id=principal.principal_id)

    @app.get("/api/seeding/v1/projects/{project_id}/jobs/{job_id}")
    def get_job(
        project_id: str,
        job_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "admin")
        service.authorize_project(project_id, principal.principal_id, write=False)
        job = store.get_job(job_id)
        if job["project_id"] != project_id:
            raise SeedingError("JOB_NOT_FOUND", "job not found", status_code=404)
        return job

    @app.post("/api/seeding/v1/projects/{project_id}/jobs/{job_id}/cancel")
    def cancel_job(
        project_id: str,
        job_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("operator", "admin")
        service.authorize_project(project_id, principal.principal_id, write=True)
        job = store.get_job(job_id)
        if job["project_id"] != project_id:
            raise SeedingError("JOB_NOT_FOUND", "job not found", status_code=404)
        return store.cancel_job(job_id)

    @app.post(
        "/api/seeding/v1/projects/{project_id}/jobs/{job_id}/resume", status_code=202
    )
    def resume_job(
        project_id: str,
        job_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("operator", "admin")
        service.authorize_project(project_id, principal.principal_id, write=True)
        job = store.get_job(job_id)
        if job["project_id"] != project_id:
            raise SeedingError("JOB_NOT_FOUND", "job not found", status_code=404)
        return store.resume_job(job_id)

    @app.post("/api/seeding/v1/projects/{project_id}/authorize-test-write")
    @app.post("/api/seeding/v1/projects/{project_id}/execute")
    @app.post("/api/seeding/v1/projects/{project_id}/release-grants")
    def locked_platform_action(
        project_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> None:
        principal.require("executor", "admin")
        service.authorize_project(project_id, principal.principal_id, write=True)
        service.locked_execution()

    return app
