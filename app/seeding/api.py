"""FastAPI surface for SEEDING planning and guarded execution."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .auth import DenyAllAuthenticator, Principal, StaticAuthenticator
from .alerts import AlertDispatcher, AlertSink
from .contracts import (
    ConfirmRequest,
    ExecuteRequest,
    IssueExecutionGrantRequest,
    IssuePausedCreateGrantRequest,
    IssueReleaseGrantRequest,
    JuguangSourceSyncRequest,
    LingxiAudienceImportRequest,
    PrepareRequest,
    ProjectConfig,
    ProjectMemberRequest,
    ReleaseEvaluationRequest,
    ReleaseExecuteRequest,
    RevokeGrantRequest,
)
from .errors import SeedingError
from .execution import TestExecutionEngine
from .grants import GrantSigner
from .juguang import JuguangClient
from .release import ProductionReleaseEngine
from .runtime import runtime_capabilities
from .service import SeedingService
from .store import SeedingStore
from .worker import DurableWorker


def authenticator_from_environment() -> StaticAuthenticator:
    filename = os.getenv("SEEDING_OPERATOR_AUTH_FILE", "").strip()
    if filename:
        try:
            raw = Path(filename).read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("SEEDING_OPERATOR_AUTH_FILE cannot be read") from exc
    else:
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
    execution_engine: Optional[TestExecutionEngine] = None,
    production_create_engine: Optional[TestExecutionEngine] = None,
    release_engine: Optional[ProductionReleaseEngine] = None,
    juguang_read_client: Optional[JuguangClient] = None,
    alert_sink: Optional[AlertSink] = None,
    grant_signer: Optional[GrantSigner] = None,
    start_worker: bool = True,
) -> FastAPI:
    store = SeedingStore(database_path)
    service = SeedingService(
        store,
        execution_engine=execution_engine,
        production_create_engine=production_create_engine,
        release_engine=release_engine,
        juguang_read_client=juguang_read_client,
        grant_signer=grant_signer,
    )
    auth = authenticator or DenyAllAuthenticator()
    worker_id = "api-" + uuid.uuid4().hex[:12]
    dispatcher = (
        AlertDispatcher(store, alert_sink, worker_id=worker_id + "-alerts")
        if alert_sink is not None
        else None
    )
    worker = DurableWorker(
        service,
        worker_id=worker_id,
        alert_dispatcher=dispatcher,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop()
            if juguang_read_client is not None:
                juguang_read_client.close()

    app = FastAPI(
        title="SEEDING Planning and Guarded Execution API",
        version="0.2.0",
        description=(
            "SEEDING planning, deterministic Juguang payload compilation, durable jobs, "
            "and grant-gated test execution. Formal release remains fail-closed."
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
            "version": "0.2.0",
            "strategy_mode": "SEEDING",
            "platform_adapter_mounted": execution_engine is not None,
            "juguang_read_adapter_mounted": juguang_read_client is not None,
            "production_create_adapter_mounted": production_create_engine is not None,
            "release_adapter_mounted": release_engine is not None,
            "alert_sink_mounted": alert_sink is not None,
            "grant_signer_mounted": grant_signer is not None,
            "formal_execution_locked": True,
            "platform_business_write_count": store.count_execution_attempts(),
        }

    @app.get("/console", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(
            Path(__file__).with_name("static") / "console.html",
            media_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; connect-src 'self'; "
                    "img-src 'self' data:; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/ready")
    def ready() -> JSONResponse:
        capabilities = runtime_capabilities(
            authenticator_mounted=not isinstance(auth, DenyAllAuthenticator),
            juguang_read_mounted=juguang_read_client is not None,
            execution_mounted=execution_engine is not None,
            production_create_mounted=production_create_engine is not None,
            release_mounted=release_engine is not None,
            grant_signer_mounted=grant_signer is not None,
        )
        content = {
            "ready_for_planning": capabilities["planning"]["ready"],
            "capabilities": capabilities,
        }
        return JSONResponse(
            status_code=200 if content["ready_for_planning"] else 503,
            content=content,
        )

    @app.get("/api/seeding/v1/ops/metrics")
    def operational_metrics(
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("admin")
        return store.operational_metrics()

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

    @app.get("/api/seeding/v1/projects/{project_id}/members")
    def list_project_members(
        project_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "executor", "admin")
        service.authorize_project(project_id, principal.principal_id, write=False)
        return {"items": store.list_project_members(project_id)}

    @app.put("/api/seeding/v1/projects/{project_id}/members/{principal_id}")
    def upsert_project_member(
        project_id: str,
        principal_id: str,
        request: ProjectMemberRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("admin")
        if principal_id != request.principal_id:
            raise SeedingError(
                "PRINCIPAL_ID_MISMATCH",
                "path and body principal ids must match",
                status_code=409,
            )
        return service.upsert_project_member(
            project_id, request, principal_id=principal.principal_id
        )

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

    @app.post(
        "/api/seeding/v1/projects/{project_id}/sources/juguang/sync",
        status_code=202,
    )
    def sync_juguang(
        project_id: str,
        request: JuguangSourceSyncRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("operator", "admin")
        job = service.enqueue_juguang_sync(
            project_id, request, principal_id=principal.principal_id
        )
        return {
            "job_id": job["job_id"],
            "operation_id": job["job_id"],
            "status": job["status"],
            "status_url": f"/api/seeding/v1/projects/{project_id}/jobs/{job['job_id']}",
        }

    @app.post(
        "/api/seeding/v1/projects/{project_id}/sources/lingxi/import",
        status_code=201,
    )
    def import_lingxi(
        project_id: str,
        request: LingxiAudienceImportRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("operator", "admin")
        return service.import_lingxi_audience_metrics(
            project_id, request, principal_id=principal.principal_id
        )

    @app.get("/api/seeding/v1/projects/{project_id}/sources")
    def list_sources(
        project_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "admin")
        service.authorize_project(project_id, principal.principal_id, write=False)
        return {
            "items": [
                item
                for item in store.list_artifacts(project_id)
                if item["kind"]
                in {
                    "juguang_source_sync",
                    "lingxi_audience_metrics",
                }
            ]
        }

    @app.get("/api/seeding/v1/projects/{project_id}/preview")
    def preview_project(
        project_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "admin")
        return service.preview(project_id, principal_id=principal.principal_id)

    @app.get("/api/seeding/v1/projects/{project_id}/audit")
    def project_audit(
        project_id: str,
        limit: int = 200,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "executor", "admin")
        service.authorize_project(project_id, principal.principal_id, write=False)
        return store.audit_trail(project_id, limit=limit)

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

    @app.get("/api/seeding/v1/projects/{project_id}/jobs")
    def list_jobs(
        project_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "executor", "admin")
        service.authorize_project(project_id, principal.principal_id, write=False)
        return {"items": store.list_jobs(project_id)}

    @app.get("/api/seeding/v1/operations/{job_id}")
    def get_operation(
        job_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "executor", "admin")
        job = store.get_job(job_id)
        service.authorize_project(
            job["project_id"], principal.principal_id, write=False
        )
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

    @app.post(
        "/api/seeding/v1/projects/{project_id}/jobs/{job_id}/reconcile",
        status_code=202,
    )
    def reconcile_job(
        project_id: str,
        job_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("executor", "admin")
        job = service.enqueue_reconcile(
            project_id, job_id, principal_id=principal.principal_id
        )
        return {
            "job_id": job["job_id"],
            "operation_id": job["job_id"],
            "status": job["status"],
            "status_url": f"/api/seeding/v1/projects/{project_id}/jobs/{job['job_id']}",
        }

    @app.post("/api/seeding/v1/projects/{project_id}/execution-grants", status_code=201)
    @app.post(
        "/api/seeding/v1/projects/{project_id}/authorize-test-write", status_code=201
    )
    def issue_execution_grant(
        project_id: str,
        request: IssueExecutionGrantRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("approver", "admin")
        role = "admin" if "admin" in principal.roles else "approver"
        return service.issue_execution_grant(
            project_id,
            request,
            principal_id=principal.principal_id,
            principal_role=role,
        )

    @app.get("/api/seeding/v1/projects/{project_id}/execution-grants/{grant_id}")
    def get_execution_grant(
        project_id: str,
        grant_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "executor", "admin")
        return service.get_grant(
            project_id, grant_id, principal_id=principal.principal_id
        )

    @app.post(
        "/api/seeding/v1/projects/{project_id}/execution-grants/{grant_id}/revoke"
    )
    def revoke_execution_grant(
        project_id: str,
        grant_id: str,
        request: RevokeGrantRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("approver", "admin")
        return service.revoke_grant(
            project_id,
            grant_id,
            principal_id=principal.principal_id,
            reason=request.reason,
        )

    @app.post("/api/seeding/v1/projects/{project_id}/execute", status_code=202)
    def execute_test(
        project_id: str,
        request: ExecuteRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("executor", "admin")
        job = service.enqueue_test_execution(
            project_id, request, principal_id=principal.principal_id
        )
        return {
            "job_id": job["job_id"],
            "operation_id": job["job_id"],
            "status": job["status"],
            "status_url": f"/api/seeding/v1/projects/{project_id}/jobs/{job['job_id']}",
        }

    @app.get("/api/seeding/v1/projects/{project_id}/execution-attempts")
    def execution_attempts(
        project_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "executor", "admin")
        service.authorize_project(project_id, principal.principal_id, write=False)
        return {"items": store.list_execution_attempts(project_id)}

    @app.post(
        "/api/seeding/v1/projects/{project_id}/production-create-grants",
        status_code=201,
    )
    def issue_production_create_grant(
        project_id: str,
        request: IssuePausedCreateGrantRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("approver", "admin")
        role = "admin" if "admin" in principal.roles else "approver"
        return service.issue_paused_create_grant(
            project_id,
            request,
            principal_id=principal.principal_id,
            principal_role=role,
        )

    @app.post(
        "/api/seeding/v1/projects/{project_id}/production-create",
        status_code=202,
    )
    def execute_production_create(
        project_id: str,
        request: ExecuteRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("executor", "admin")
        job = service.enqueue_production_create(
            project_id, request, principal_id=principal.principal_id
        )
        return {
            "job_id": job["job_id"],
            "operation_id": job["job_id"],
            "status": job["status"],
            "status_url": f"/api/seeding/v1/projects/{project_id}/jobs/{job['job_id']}",
        }

    @app.post("/api/seeding/v1/projects/{project_id}/release-grants", status_code=201)
    def issue_release_grant(
        project_id: str,
        request: IssueReleaseGrantRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("approver", "admin")
        role = "admin" if "admin" in principal.roles else "approver"
        return service.issue_release_grant(
            project_id,
            request,
            principal_id=principal.principal_id,
            principal_role=role,
        )

    @app.get("/api/seeding/v1/projects/{project_id}/release-grants/{grant_id}")
    def get_release_grant(
        project_id: str,
        grant_id: str,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("viewer", "operator", "approver", "executor", "admin")
        return service.get_grant(
            project_id, grant_id, principal_id=principal.principal_id
        )

    @app.post("/api/seeding/v1/projects/{project_id}/release-grants/{grant_id}/revoke")
    def revoke_release_grant(
        project_id: str,
        grant_id: str,
        request: RevokeGrantRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("approver", "admin")
        return service.revoke_grant(
            project_id,
            grant_id,
            principal_id=principal.principal_id,
            reason=request.reason,
        )

    @app.post(
        "/api/seeding/v1/projects/{project_id}/release-grants/{grant_id}/execute",
        status_code=202,
    )
    def execute_release(
        project_id: str,
        grant_id: str,
        request: ReleaseExecuteRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("executor", "admin")
        job = service.enqueue_release(
            project_id,
            grant_id,
            request,
            principal_id=principal.principal_id,
        )
        return {
            "job_id": job["job_id"],
            "operation_id": job["job_id"],
            "status": job["status"],
            "status_url": f"/api/seeding/v1/projects/{project_id}/jobs/{job['job_id']}",
        }

    @app.post(
        "/api/seeding/v1/projects/{project_id}/release-grants/{grant_id}/evaluate",
        status_code=202,
    )
    def evaluate_release(
        project_id: str,
        grant_id: str,
        request: ReleaseEvaluationRequest,
        principal: Principal = Depends(auth.dependency),
    ) -> Dict[str, Any]:
        principal.require("operator", "executor", "admin")
        job = service.enqueue_release_evaluation(
            project_id,
            grant_id,
            request,
            principal_id=principal.principal_id,
        )
        return {
            "job_id": job["job_id"],
            "operation_id": job["job_id"],
            "status": job["status"],
            "status_url": f"/api/seeding/v1/projects/{project_id}/jobs/{job['job_id']}",
        }

    return app
