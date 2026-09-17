"""Application service for deterministic planning and guarded execution."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple

from .algorithms import uniform_budget_capacity
from .contracts import (
    ConfirmRequest,
    AudienceCandidate,
    ExecuteRequest,
    ExecutionGrant,
    FeedPlanCandidate,
    IssueExecutionGrantRequest,
    IssuePausedCreateGrantRequest,
    IssueReleaseGrantRequest,
    JuguangSourceSyncRequest,
    LingxiAudienceImportRequest,
    PausedCreateGrant,
    PairCandidate,
    PlanIdentity,
    PrepareRequest,
    ProjectConfig,
    ProjectMemberRequest,
    ReleaseEvaluationRequest,
    ReleaseExecuteRequest,
    ReleaseGrant,
    ReleaseObjectBinding,
    SearchPlanCandidate,
    StaticDmpSnapshot,
)
from .decision import evaluate_audiences, evaluate_keywords, search_release_allowed
from .errors import ExecutionLocked, SeedingError
from .execution import ExecutionPlanInput, TestExecutionEngine
from .grants import GrantSigner
from .identity import logical_plan_key, sha256_json
from .juguang_compiler import compile_feed_plan, compile_search_plan
from .juguang import JuguangClient
from .release import ProductionReleaseEngine
from .sources import sync_juguang_sources
from .state import DeliveryState, ExecutionState, ProjectState
from .store import SeedingStore

MAX_PAIR_COMBINATIONS = 200_000


class SeedingService:
    def __init__(
        self,
        store: SeedingStore,
        *,
        execution_engine: TestExecutionEngine | None = None,
        production_create_engine: TestExecutionEngine | None = None,
        release_engine: ProductionReleaseEngine | None = None,
        juguang_read_client: JuguangClient | None = None,
        grant_signer: GrantSigner | None = None,
    ) -> None:
        self.store = store
        self.execution_engine = execution_engine
        self.production_create_engine = production_create_engine
        self.release_engine = release_engine
        self.juguang_read_client = juguang_read_client
        self.grant_signer = grant_signer

    def create_project(
        self, config: ProjectConfig, *, principal_id: str
    ) -> Dict[str, Any]:
        return self.store.create_project(config, owner_principal_id=principal_id)

    def authorize_project(
        self,
        project_id: str,
        principal_id: str,
        *,
        write: bool,
        required_access: str | None = None,
    ) -> Dict[str, Any]:
        project = self.store.get_project(project_id)
        member = self.store.get_project_member(project_id, principal_id)
        if member is None:
            raise SeedingError(
                "RBAC_FORBIDDEN", "project access denied", status_code=403
            )
        allowed_by_capability = {
            "WRITE": {"WRITE", "APPROVE", "EXECUTE", "ADMIN"},
            "APPROVE": {"APPROVE", "ADMIN"},
            "EXECUTE": {"EXECUTE", "ADMIN"},
            "ADMIN": {"ADMIN"},
        }
        if required_access is not None:
            allowed_levels = allowed_by_capability.get(required_access)
            if allowed_levels is None:
                raise ValueError("unknown project access capability")
            if member["access_level"] not in allowed_levels:
                raise SeedingError(
                    "RBAC_FORBIDDEN",
                    f"project {required_access.lower()} access denied",
                    status_code=403,
                )
            return project
        if write and member["access_level"] not in {
            "WRITE",
            "APPROVE",
            "EXECUTE",
            "ADMIN",
        }:
            raise SeedingError(
                "RBAC_FORBIDDEN", "project write access denied", status_code=403
            )
        return project

    def upsert_project_member(
        self,
        project_id: str,
        request: ProjectMemberRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.store.get_project(project_id)
        member = self.store.get_project_member(project_id, principal_id)
        if member is None or member["access_level"] != "ADMIN":
            raise SeedingError(
                "RBAC_FORBIDDEN", "project admin access required", status_code=403
            )
        if (
            request.principal_id == project["owner_principal_id"]
            and request.access_level != "ADMIN"
        ):
            raise SeedingError(
                "PROJECT_OWNER_ACCESS_IMMUTABLE",
                "the project owner must retain ADMIN access",
                status_code=409,
            )
        return self.store.upsert_project_member(
            project_id=project_id,
            principal_id=request.principal_id,
            access_level=request.access_level,
            added_by=principal_id,
        )

    def enqueue_prepare(
        self,
        project_id: str,
        request: PrepareRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(project_id, principal_id, write=True)
        if project["config_sha"] != request.config_sha:
            raise SeedingError(
                "CONFIG_SHA_MISMATCH",
                "prepare request does not match the current project revision",
                status_code=409,
            )
        self._validate_parameter_set(project, request)
        existing = self.store.find_job(
            project_id=project_id,
            kind="PREPARE",
            input_sha=sha256_json(request.model_dump(mode="json", by_alias=True)),
        )
        if existing is not None:
            return existing
        if project["project_state"] == ProjectState.DRAFT.value:
            self.store.transition_project(
                project_id,
                ProjectState.INPUT_VALIDATED,
                actor_id=principal_id,
                reason_code="PREPARE_REQUEST_VALIDATED",
            )
        elif project["project_state"] not in {
            ProjectState.INPUT_VALIDATED.value,
            ProjectState.ADVISORY_READY.value,
            ProjectState.FAILED_CLOSED.value,
            ProjectState.WAITING_BUDGET.value,
        }:
            raise SeedingError(
                "PROJECT_NOT_PREPARABLE",
                "project is not in a preparable state",
                status_code=409,
                details={"project_state": project["project_state"]},
            )
        return self.store.enqueue_job(
            project_id=project_id,
            kind="PREPARE",
            payload=request.model_dump(mode="json", by_alias=True),
        )

    def process_next_job(self, *, worker_id: str) -> Dict[str, Any] | None:
        job = self.store.claim_job(worker_id=worker_id)
        if job is None:
            return None
        try:
            if job["kind"] == "PREPARE":
                result = self._process_prepare(job)
            elif job["kind"] == "EXECUTE_TEST":
                result = self._process_test_execution(job)
            elif job["kind"] == "RECONCILE":
                result = self._process_reconcile(job)
            elif job["kind"] == "PRODUCTION_CREATE":
                result = self._process_production_create(job)
            elif job["kind"] == "RELEASE":
                result = self._process_release(job)
            elif job["kind"] == "RELEASE_EVALUATE":
                result = self._process_release_evaluation(job)
            elif job["kind"] == "RELEASE_RECOVERY":
                result = self._process_release_recovery(job)
            elif job["kind"] == "SYNC_JUGUANG":
                result = self._process_juguang_sync(job)
            else:
                raise SeedingError("UNKNOWN_JOB_KIND", "unknown durable job kind")
            if (
                job["kind"] in {"EXECUTE_TEST", "PRODUCTION_CREATE", "RECONCILE"}
                and result.get("status") == "RECONCILE_REQUIRED"
            ):
                self.store.require_reconcile_job(
                    job["job_id"], worker_id=worker_id, result=result
                )
            elif job["kind"] in {
                "EXECUTE_TEST",
                "PRODUCTION_CREATE",
                "RECONCILE",
            } and result.get("status") not in {
                "VERIFIED_PAUSED",
                "RECONCILED_VERIFIED_PAUSED",
                "RECONCILED_NOT_CREATED",
            }:
                self.store.fail_terminal_job(
                    job["job_id"], worker_id=worker_id, result=result
                )
            elif job["kind"] == "RELEASE" and result.get("status") != "RELEASE_ACTIVE":
                self.store.emit_event(
                    project_id=job["project_id"],
                    event_type="ALERT",
                    payload={
                        "severity": "CRITICAL",
                        "code": result.get("status"),
                        "job_id": job["job_id"],
                    },
                )
                self.store.fail_terminal_job(
                    job["job_id"], worker_id=worker_id, result=result
                )
            elif (
                job["kind"] == "RELEASE_EVALUATE"
                and result.get("status") == "CAP_EXCEEDED_RELOCK_FAILED"
            ):
                self.store.fail_terminal_job(
                    job["job_id"], worker_id=worker_id, result=result
                )
            elif (
                job["kind"] == "RELEASE_RECOVERY"
                and result.get("status") != "RELEASE_RECOVERED_SAFE"
            ):
                self.store.emit_event(
                    project_id=job["project_id"],
                    event_type="ALERT",
                    payload={
                        "severity": "CRITICAL",
                        "code": "RELEASE_RECOVERY_FAILED",
                        "job_id": job["job_id"],
                    },
                )
                self.store.fail_terminal_job(
                    job["job_id"], worker_id=worker_id, result=result
                )
            else:
                self.store.complete_job(
                    job["job_id"], worker_id=worker_id, result=result
                )
            return result
        except Exception as exc:
            external_jobs = {
                "EXECUTE_TEST",
                "PRODUCTION_CREATE",
                "RECONCILE",
                "RELEASE",
                "RELEASE_EVALUATE",
                "RELEASE_RECOVERY",
            }
            if job["kind"] in external_jobs:
                self.store.require_reconcile_job(
                    job["job_id"],
                    worker_id=worker_id,
                    result={
                        "status": "RECONCILE_REQUIRED",
                        "error_code": "EXTERNAL_OPERATION_WORKER_EXCEPTION",
                        "error_type": type(exc).__name__,
                    },
                )
            else:
                self.store.fail_job(
                    job["job_id"], worker_id=worker_id, error=str(exc)[:2000]
                )
            if job["kind"] in {"EXECUTE_TEST", "PRODUCTION_CREATE", "RECONCILE"}:
                try:
                    state = self.store.get_project(job["project_id"])["execution_state"]
                    if state == ExecutionState.WRITE_IN_PROGRESS.value:
                        self.store.transition_execution(
                            job["project_id"],
                            ExecutionState.RECONCILE_REQUIRED,
                            actor_id="durable-worker",
                            reason_code="EXTERNAL_OPERATION_WORKER_EXCEPTION",
                        )
                except Exception:
                    pass
            if job["kind"] in {"RELEASE", "RELEASE_EVALUATE", "RELEASE_RECOVERY"}:
                self.store.emit_event(
                    project_id=job["project_id"],
                    event_type="ALERT",
                    payload={
                        "severity": "CRITICAL",
                        "code": "EXTERNAL_OPERATION_WORKER_EXCEPTION",
                        "job_id": job["job_id"],
                    },
                )
            try:
                state = self.store.get_project(job["project_id"])["project_state"]
                if state in {
                    ProjectState.INPUT_VALIDATED.value,
                    ProjectState.THREE_CHAIN_READY.value,
                    ProjectState.MATRIX_READY.value,
                    ProjectState.WAITING_CONFIRMATION.value,
                }:
                    self.store.transition_project(
                        job["project_id"],
                        ProjectState.FAILED_CLOSED,
                        actor_id="durable-worker",
                        reason_code="PREPARE_JOB_FAILED",
                    )
            except Exception:
                pass
            raise

    def enqueue_juguang_sync(
        self,
        project_id: str,
        request: JuguangSourceSyncRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(project_id, principal_id, write=True)
        if self.juguang_read_client is None:
            raise SeedingError(
                "JUGUANG_READ_ADAPTER_NOT_CONFIGURED",
                "Juguang Access-Token client is not configured",
                status_code=503,
            )
        payload = {
            "config_sha": project["config_sha"],
            "request": request.model_dump(mode="json", by_alias=True),
        }
        return self.store.enqueue_job(
            project_id=project_id,
            kind="SYNC_JUGUANG",
            payload=payload,
            max_attempts=3,
        )

    def _process_juguang_sync(self, job: Dict[str, Any]) -> Dict[str, Any]:
        if self.juguang_read_client is None:
            raise SeedingError(
                "JUGUANG_READ_ADAPTER_NOT_CONFIGURED",
                "Juguang read adapter is unavailable",
                status_code=503,
            )
        project = self.store.get_project(job["project_id"])
        if job["input"]["config_sha"] != project["config_sha"]:
            raise SeedingError(
                "CONFIG_SHA_MISMATCH",
                "project changed after source sync was queued",
                status_code=409,
            )
        advertiser = project["config"]["advertiser"]
        request = JuguangSourceSyncRequest.model_validate(job["input"]["request"])
        result = sync_juguang_sources(
            client=self.juguang_read_client,
            advertiser_id=int(advertiser["advertiser_id"]),
            account_scope=advertiser["account_id"],
            request=request,
        )
        return self.store.put_artifact(
            project_id=job["project_id"],
            kind="juguang_source_sync",
            input_sha=job["input_sha"],
            content=result,
        )

    def import_lingxi_audience_metrics(
        self,
        project_id: str,
        request: LingxiAudienceImportRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(project_id, principal_id, write=True)
        sync = self.store.latest_artifact(project_id, "juguang_source_sync")
        catalog = {item["value"]: item for item in sync["content"]["audience_catalog"]}
        unknown = sorted(
            item.package_value
            for item in request.rows
            if item.package_value not in catalog
        )
        if unknown:
            raise SeedingError(
                "AUDIENCE_PACKAGE_NOT_DELIVERABLE",
                "Lingxi metrics contain packages not present in the latest deliverable Juguang catalog",
                status_code=409,
                details={"package_values": unknown[:50]},
            )
        created_at = datetime.now(timezone.utc)
        candidates = []
        for row in request.rows:
            platform = catalog[row.package_value]
            if (
                platform.get("name") != row.package_name
                or platform.get("group_id") != row.group_id
            ):
                raise SeedingError(
                    "AUDIENCE_IDENTITY_MISMATCH",
                    "Lingxi and Juguang package identity do not match",
                    status_code=409,
                    details={"package_value": row.package_value},
                )
            asset = StaticDmpSnapshot(
                project_id=project_id,
                contract_version="seeding.static_dmp_snapshot.v1",
                config_sha256=project["config_sha"],
                source_proof_sha256=row.source_proof_sha256,
                created_at=created_at,
                package_id=row.package_value,
                package_name=row.package_name,
                group_id=row.group_id,
                snapshot_at=row.snapshot_at,
                population=row.population,
                aips=row.aips,
                **{"I+TI": row.i_ti},
                taxonomy_version=row.taxonomy_version,
                expires_at=row.expires_at,
            )
            candidate = AudienceCandidate(
                audience_id="aud_"
                + sha256_json(
                    {
                        "package_value": row.package_value,
                        "snapshot_at": row.snapshot_at.isoformat(),
                    }
                )[:24],
                audience_mode="STATIC_DMP",
                source_system="LINGXI_IMPORT",
                metric_state="READY",
                population=row.population,
                hard_gates={
                    "availability": True,
                    "size": True,
                    "quality": True,
                    "policy": True,
                    "relevance": True,
                },
                score_version="pending-prepare",
                asset=asset,
            )
            candidates.append(candidate.model_dump(mode="json", by_alias=True))
        content = {
            "schema": "seeding.lingxi_audience_import.v1",
            "source_ref": request.source_ref,
            "juguang_catalog_sha256": sync["content_sha"],
            "audiences": candidates,
        }
        return self.store.put_artifact(
            project_id=project_id,
            kind="lingxi_audience_metrics",
            input_sha=sha256_json(request),
            content=content,
        )

    def issue_execution_grant(
        self,
        project_id: str,
        request: IssueExecutionGrantRequest,
        *,
        principal_id: str,
        principal_role: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(
            project_id, principal_id, write=True, required_access="APPROVE"
        )
        if self.grant_signer is None:
            raise SeedingError(
                "GRANT_SIGNER_NOT_CONFIGURED",
                "grant signer is not configured",
                status_code=503,
            )
        if project["project_state"] != ProjectState.ADVISORY_READY.value:
            raise SeedingError(
                "PROJECT_NOT_CONFIRMED",
                "project must be confirmed before a test execution grant is issued",
                status_code=409,
            )
        config = project["config"]
        advertiser = config["advertiser"]
        if advertiser["environment"] != "TEST":
            raise SeedingError(
                "ENVIRONMENT_SCOPE_MISMATCH",
                "ExecutionGrant is restricted to a TEST advertiser",
                status_code=409,
            )
        if request.config_sha != project["config_sha"]:
            raise SeedingError(
                "CONFIG_SHA_MISMATCH", "grant config hash is stale", status_code=409
            )
        matrix = self.store.latest_artifact(project_id, "plan_matrix")
        confirmation = self.store.latest_artifact(project_id, "confirmation")
        if request.matrix_sha != matrix["content_sha"]:
            raise SeedingError(
                "MATRIX_SHA_MISMATCH", "grant matrix hash is stale", status_code=409
            )
        if request.confirmation_sha != confirmation["content_sha"]:
            raise SeedingError(
                "CONFIRMATION_SHA_MISMATCH",
                "grant confirmation hash is stale",
                status_code=409,
            )
        plans = sorted(
            self.store.list_active_plans(project_id),
            key=lambda item: item["plan_revision_id"],
        )
        if not plans or any(
            "platform_payload" not in item["payload"] for item in plans
        ):
            raise SeedingError(
                "PLATFORM_PAYLOAD_NOT_COMPILED",
                "every active plan must have a verified Juguang payload",
                status_code=409,
            )
        expected_plan_ids = tuple(item["plan_revision_id"] for item in plans)
        expected_object_hashes = tuple(
            sorted(sha256_json(item["payload"]) for item in plans)
        )
        expected_platform_hashes = tuple(
            item["payload"]["platform_payload_sha256"] for item in plans
        )
        if request.plan_revision_ids != expected_plan_ids:
            raise SeedingError(
                "PLAN_REVISION_MISMATCH",
                "grant plan revisions are stale",
                status_code=409,
            )
        if tuple(sorted(request.object_hashes)) != expected_object_hashes:
            raise SeedingError(
                "OBJECT_HASH_MISMATCH", "grant object hashes are stale", status_code=409
            )
        if request.platform_payload_hashes != expected_platform_hashes:
            raise SeedingError(
                "PLATFORM_PAYLOAD_SHA_MISMATCH",
                "grant platform payload hashes are stale",
                status_code=409,
            )
        issued_at = datetime.now(timezone.utc)
        if request.expires_at <= issued_at:
            raise SeedingError(
                "GRANT_EXPIRY_INVALID",
                "execution grant expiry must be in the future",
                status_code=422,
            )
        unsigned = {
            "grant_id": "execgrant_" + uuid.uuid4().hex,
            "project_id": project_id,
            "environment": "TEST",
            "platform": "XIAOHONGSHU_JUGUANG",
            "advertiser_id": advertiser["advertiser_id"],
            "account_id": advertiser["account_id"],
            "authorization_domain_id": advertiser["authorization_domain_id"],
            "allowed_actions": ("CREATE", "PAUSE", "READBACK", "RELOCK", "RECONCILE"),
            "plan_revision_ids": request.plan_revision_ids,
            "object_hashes": tuple(sorted(request.object_hashes)),
            "platform_payload_hashes": request.platform_payload_hashes,
            "config_sha": request.config_sha,
            "matrix_sha": request.matrix_sha,
            "confirmation_sha": request.confirmation_sha,
            "max_attempts": request.max_attempts,
            "issued_at": issued_at.isoformat(),
            "expires_at": request.expires_at.isoformat(),
            "nonce": request.nonce,
            "approver_id": principal_id,
            "approver_role": principal_role,
        }
        normalized_unsigned = ExecutionGrant.model_validate(
            {
                **unsigned,
                "grant_sha256": "0" * 64,
                "signature_sha256": "0" * 64,
            }
        ).model_dump(
            mode="json",
            exclude_none=True,
            exclude={"grant_sha256", "signature_sha256"},
        )
        grant_sha = sha256_json(normalized_unsigned)
        signed = {**normalized_unsigned, "grant_sha256": grant_sha}
        grant = ExecutionGrant.model_validate(
            {**signed, "signature_sha256": self.grant_signer.sign(signed)}
        )
        stored = self.store.issue_grant(
            grant_id=grant.grant_id,
            project_id=project_id,
            grant_type="EXECUTION",
            payload=grant.model_dump(mode="json", exclude_none=True),
            expires_at=grant.expires_at,
        )
        current = project["execution_state"]
        if current in {
            ExecutionState.NONE.value,
            ExecutionState.READBACK_VERIFIED.value,
            ExecutionState.RECOVERY_REQUIRED.value,
        }:
            self.store.transition_execution(
                project_id,
                ExecutionState.TEST_WRITE_AUTHORIZED,
                actor_id=principal_id,
                reason_code="EXECUTION_GRANT_ISSUED",
            )
        else:
            # Do not leave an active grant behind if the execution state cannot consume it.
            self.store.revoke_grant(
                grant.grant_id,
                actor_id="system",
                reason=f"execution state {current} cannot accept a new grant",
            )
            raise SeedingError(
                "EXECUTION_STATE_CONFLICT",
                "project execution state cannot accept a new grant",
                status_code=409,
            )
        return stored

    def get_grant(
        self, project_id: str, grant_id: str, *, principal_id: str
    ) -> Dict[str, Any]:
        self.authorize_project(project_id, principal_id, write=False)
        grant = self.store.get_grant(grant_id)
        if grant["project_id"] != project_id:
            raise SeedingError("GRANT_NOT_FOUND", "grant not found", status_code=404)
        return grant

    def revoke_grant(
        self,
        project_id: str,
        grant_id: str,
        *,
        principal_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        self.authorize_project(
            project_id, principal_id, write=True, required_access="APPROVE"
        )
        grant = self.get_grant(project_id, grant_id, principal_id=principal_id)
        result = self.store.revoke_grant(grant_id, actor_id=principal_id, reason=reason)
        project = self.store.get_project(project_id)
        if (
            grant["grant_type"] == "EXECUTION"
            and project["execution_state"] == ExecutionState.TEST_WRITE_AUTHORIZED.value
        ):
            self.store.transition_execution(
                project_id,
                ExecutionState.NONE,
                actor_id=principal_id,
                reason_code="EXECUTION_GRANT_REVOKED",
            )
        return result

    def enqueue_test_execution(
        self,
        project_id: str,
        request: ExecuteRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(
            project_id, principal_id, write=True, required_access="EXECUTE"
        )
        if self.execution_engine is None:
            raise SeedingError(
                "PLATFORM_ADAPTER_NOT_CONFIGURED",
                "Juguang execution and account relock adapters are not configured",
                status_code=503,
            )
        if self.grant_signer is None:
            raise SeedingError(
                "GRANT_SIGNER_NOT_CONFIGURED",
                "grant signer is not configured",
                status_code=503,
            )
        stored = self.get_grant(project_id, request.grant_id, principal_id=principal_id)
        grant = ExecutionGrant.model_validate(stored["payload"])
        if (
            request.grant_sha256 != grant.grant_sha256
            or request.signature_sha256 != grant.signature_sha256
        ):
            raise SeedingError(
                "GRANT_INTEGRITY_MISMATCH",
                "grant proof does not match",
                status_code=409,
            )
        self._verify_execution_grant_signature(grant)
        advertiser = project["config"]["advertiser"]
        if (
            grant.advertiser_id != advertiser["advertiser_id"]
            or grant.account_id != advertiser["account_id"]
            or grant.authorization_domain_id != advertiser["authorization_domain_id"]
        ):
            raise SeedingError(
                "GRANT_SCOPE_MISMATCH",
                "grant is outside the current project scope",
                status_code=409,
            )
        active = {
            item["plan_revision_id"]: item
            for item in self.store.list_active_plans(project_id)
        }
        plans: List[Dict[str, Any]] = []
        for plan_id, payload_sha in zip(
            grant.plan_revision_ids, grant.platform_payload_hashes
        ):
            row = active.get(plan_id)
            if row is None:
                raise SeedingError(
                    "PLAN_REVISION_MISMATCH",
                    "authorized plan is no longer active",
                    status_code=409,
                )
            payload = row["payload"].get("platform_payload")
            if payload is None or sha256_json(payload) != payload_sha:
                raise SeedingError(
                    "PLATFORM_PAYLOAD_SHA_MISMATCH",
                    "authorized platform payload changed",
                    status_code=409,
                )
            plans.append(
                {
                    "plan_revision_id": plan_id,
                    "logical_plan_key": row["logical_plan_key"],
                    "payload": payload,
                    "payload_sha256": payload_sha,
                }
            )
        job_payload = {
            "grant_id": grant.grant_id,
            "grant_sha256": grant.grant_sha256,
            "advertiser_id": grant.advertiser_id,
            "environment": "TEST",
            "plans": plans,
        }
        existing = self.store.find_job(
            project_id=project_id,
            kind="EXECUTE_TEST",
            input_sha=sha256_json(job_payload),
        )
        if existing is not None:
            return existing
        if project["execution_state"] != ExecutionState.TEST_WRITE_AUTHORIZED.value:
            raise SeedingError(
                "GRANT_SCOPE_MISMATCH",
                "project is not in the authorized test-write state",
                status_code=409,
            )
        # External write jobs are never automatically retried. A worker crash or
        # timeout transitions to explicit reconcile instead of creating duplicates.
        job = self.store.consume_grant_and_enqueue_job(
            grant_id=grant.grant_id,
            expected_grant_type="EXECUTION",
            project_id=project_id,
            kind="EXECUTE_TEST",
            payload=job_payload,
            max_attempts=1,
        )
        self.store.transition_execution(
            project_id,
            ExecutionState.WRITE_IN_PROGRESS,
            actor_id=principal_id,
            reason_code="EXECUTION_GRANT_CONSUMED",
        )
        return job

    def _process_test_execution(self, job: Dict[str, Any]) -> Dict[str, Any]:
        if self.execution_engine is None:
            raise SeedingError(
                "PLATFORM_ADAPTER_NOT_CONFIGURED",
                "execution adapter is unavailable",
                status_code=503,
            )
        payload = job["input"]
        plans = tuple(ExecutionPlanInput(**item) for item in payload["plans"])
        receipt = self.execution_engine.execute(
            advertiser_id=int(payload["advertiser_id"]), plans=plans
        )
        result = asdict(receipt)
        self.store.record_execution_result(
            project_id=job["project_id"],
            grant_id=payload["grant_id"],
            result=result,
        )
        target_by_status = {
            "VERIFIED_PAUSED": ExecutionState.READBACK_VERIFIED,
            "RECONCILE_REQUIRED": ExecutionState.RECONCILE_REQUIRED,
            "PAUSE_FAILED_EMERGENCY": ExecutionState.PAUSE_FAILED_EMERGENCY,
            "READBACK_MISMATCH": ExecutionState.READBACK_MISMATCH,
            "RELOCK_FAILED": ExecutionState.RELOCK_FAILED,
            "FAILED_TERMINAL": ExecutionState.RECOVERY_REQUIRED,
        }
        target = target_by_status.get(receipt.status, ExecutionState.RECOVERY_REQUIRED)
        self.store.transition_execution(
            job["project_id"],
            target,
            actor_id="durable-worker",
            reason_code=f"TEST_EXECUTION_{receipt.status}",
        )
        return result

    def enqueue_reconcile(
        self,
        project_id: str,
        source_job_id: str,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(
            project_id, principal_id, write=True, required_access="EXECUTE"
        )
        if self.grant_signer is None:
            raise SeedingError(
                "PLATFORM_ADAPTER_NOT_CONFIGURED",
                "execution, relock, and grant adapters are required for reconciliation",
                status_code=503,
            )
        source = self.store.get_job(source_job_id)
        if (
            source["project_id"] == project_id
            and source["kind"] in {"RELEASE", "RELEASE_EVALUATE", "RELEASE_RECOVERY"}
            and source["status"] == "RECONCILE_REQUIRED"
        ):
            return self._enqueue_release_recovery(
                project_id=project_id,
                source=source,
                principal_id=principal_id,
            )
        if (
            source["project_id"] != project_id
            or source["kind"] not in {"EXECUTE_TEST", "PRODUCTION_CREATE", "RECONCILE"}
            or source["status"] != "RECONCILE_REQUIRED"
        ):
            raise SeedingError(
                "JOB_NOT_RECONCILABLE",
                "only an indeterminate external-write job can be reconciled",
                status_code=409,
            )
        original = source["input"]
        if source["kind"] == "RECONCILE":
            original = original["execution_input"]
        required_engine = (
            self.production_create_engine
            if original.get("environment") == "PRODUCTION"
            else self.execution_engine
        )
        if required_engine is None:
            raise SeedingError(
                "PLATFORM_ADAPTER_NOT_CONFIGURED",
                "environment-specific execution adapter is required for reconciliation",
                status_code=503,
            )
        grant_row = self.store.get_grant(original["grant_id"])
        grant = (
            PausedCreateGrant.model_validate(grant_row["payload"])
            if grant_row["grant_type"] == "PRODUCTION_CREATE"
            else ExecutionGrant.model_validate(grant_row["payload"])
        )
        if "RECONCILE" not in grant.allowed_actions:
            raise SeedingError(
                "GRANT_ACTION_NOT_ALLOWED",
                "grant does not authorize reconciliation",
                status_code=403,
            )
        self._verify_execution_grant_signature(grant)
        if project["execution_state"] != ExecutionState.RECONCILE_REQUIRED.value:
            raise SeedingError(
                "EXECUTION_STATE_CONFLICT",
                "project is not waiting for reconciliation",
                status_code=409,
            )
        return self.store.enqueue_job(
            project_id=project_id,
            kind="RECONCILE",
            payload={
                "source_job_id": source_job_id,
                "execution_input": original,
            },
            max_attempts=1,
        )

    def _enqueue_release_recovery(
        self,
        *,
        project_id: str,
        source: Dict[str, Any],
        principal_id: str,
    ) -> Dict[str, Any]:
        if self.release_engine is None:
            raise SeedingError(
                "RELEASE_ADAPTER_NOT_CONFIGURED",
                "release safety adapter is required for recovery",
                status_code=503,
            )
        original = source["input"]
        if source["kind"] == "RELEASE_RECOVERY":
            original = original["release_input"]
        grant_row = self.store.get_grant(original["grant_id"])
        grant = ReleaseGrant.model_validate(grant_row["payload"])
        self._verify_execution_grant_signature(grant)
        if "PAUSE" not in grant.allowed_actions:
            raise SeedingError(
                "GRANT_ACTION_NOT_ALLOWED",
                "release grant does not authorize emergency pause",
                status_code=403,
            )
        return self.store.enqueue_job(
            project_id=project_id,
            kind="RELEASE_RECOVERY",
            payload={
                "source_job_id": source["job_id"],
                "release_input": {
                    "grant_id": grant.release_grant_id,
                    "advertiser_id": grant.advertiser_id,
                    "campaign_ids": grant.platform_campaign_ids,
                },
                "requested_by": principal_id,
            },
            max_attempts=1,
        )

    def _process_reconcile(self, job: Dict[str, Any]) -> Dict[str, Any]:
        if self.execution_engine is None and self.production_create_engine is None:
            raise SeedingError(
                "PLATFORM_ADAPTER_NOT_CONFIGURED",
                "execution adapter is unavailable",
                status_code=503,
            )
        execution_input = job["input"]["execution_input"]
        plans = tuple(ExecutionPlanInput(**item) for item in execution_input["plans"])
        engine = (
            self.production_create_engine
            if execution_input.get("environment") == "PRODUCTION"
            else self.execution_engine
        )
        if engine is None:
            raise SeedingError(
                "PLATFORM_ADAPTER_NOT_CONFIGURED",
                "environment-specific execution adapter is unavailable",
                status_code=503,
            )
        receipt = engine.reconcile(
            advertiser_id=int(execution_input["advertiser_id"]), plans=plans
        )
        result = asdict(receipt)
        self.store.record_execution_result(
            project_id=job["project_id"],
            grant_id=execution_input["grant_id"],
            result=result,
        )
        if execution_input.get("environment") == "PRODUCTION":
            self.store.put_artifact(
                project_id=job["project_id"],
                kind="production_paused_readback",
                input_sha=job["input_sha"],
                content=result,
            )
        target_by_status = {
            "RECONCILED_VERIFIED_PAUSED": (
                ExecutionState.WAITING_RELEASE_AUTHORIZATION
                if execution_input.get("environment") == "PRODUCTION"
                else ExecutionState.READBACK_VERIFIED
            ),
            "RECONCILED_NOT_CREATED": ExecutionState.RECOVERY_REQUIRED,
            "PAUSE_FAILED_EMERGENCY": ExecutionState.RECOVERY_REQUIRED,
            "READBACK_MISMATCH": ExecutionState.RECOVERY_REQUIRED,
            "RELOCK_FAILED": ExecutionState.RELOCK_FAILED,
            "RECOVERY_REQUIRED": ExecutionState.RECOVERY_REQUIRED,
        }
        target = target_by_status.get(receipt.status)
        if target is not None:
            self.store.transition_execution(
                job["project_id"],
                target,
                actor_id="durable-worker",
                reason_code=f"RECONCILE_{receipt.status}",
            )
        return result

    def _verify_execution_grant_signature(self, grant: Any) -> None:
        if self.grant_signer is None:
            raise SeedingError(
                "GRANT_SIGNER_NOT_CONFIGURED",
                "grant signer is not configured",
                status_code=503,
            )
        signed_payload = grant.model_dump(mode="json", exclude_none=True)
        signature = signed_payload.pop("signature_sha256")
        grant_sha = signed_payload["grant_sha256"]
        unsigned_payload = dict(signed_payload)
        unsigned_payload.pop("grant_sha256")
        if sha256_json(unsigned_payload) != grant_sha or not self.grant_signer.verify(
            signed_payload, signature
        ):
            raise SeedingError(
                "GRANT_SIGNATURE_INVALID", "grant signature is invalid", status_code=409
            )

    def issue_paused_create_grant(
        self,
        project_id: str,
        request: IssuePausedCreateGrantRequest,
        *,
        principal_id: str,
        principal_role: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(
            project_id, principal_id, write=True, required_access="APPROVE"
        )
        if self.grant_signer is None:
            raise SeedingError(
                "GRANT_SIGNER_NOT_CONFIGURED",
                "grant signer is not configured",
                status_code=503,
            )
        if project["project_state"] != ProjectState.ADVISORY_READY.value:
            raise SeedingError(
                "PROJECT_NOT_CONFIRMED", "project must be confirmed", status_code=409
            )
        advertiser = project["config"]["advertiser"]
        if advertiser["environment"] != "PRODUCTION":
            raise SeedingError(
                "ENVIRONMENT_SCOPE_MISMATCH",
                "paused production creation requires a PRODUCTION advertiser",
                status_code=409,
            )
        plans, matrix, confirmation = self._validated_compiled_scope(
            project_id=project_id,
            project=project,
            config_sha=request.config_sha,
            matrix_sha=request.matrix_sha,
            confirmation_sha=request.confirmation_sha,
            object_hashes=request.object_hashes,
            plan_revision_ids=request.plan_revision_ids,
            platform_payload_hashes=request.platform_payload_hashes,
        )
        del plans, matrix, confirmation
        issued_at = datetime.now(timezone.utc)
        if request.expires_at <= issued_at:
            raise SeedingError(
                "GRANT_EXPIRY_INVALID",
                "grant expiry must be in the future",
                status_code=422,
            )
        unsigned = {
            "grant_id": "pausegrant_" + uuid.uuid4().hex,
            "project_id": project_id,
            "environment": "PRODUCTION",
            "platform": "XIAOHONGSHU_JUGUANG",
            "advertiser_id": advertiser["advertiser_id"],
            "account_id": advertiser["account_id"],
            "authorization_domain_id": advertiser["authorization_domain_id"],
            "allowed_actions": (
                "CREATE_PAUSED",
                "PAUSE",
                "READBACK",
                "RELOCK",
                "RECONCILE",
            ),
            "plan_revision_ids": request.plan_revision_ids,
            "object_hashes": tuple(sorted(request.object_hashes)),
            "platform_payload_hashes": request.platform_payload_hashes,
            "config_sha": request.config_sha,
            "matrix_sha": request.matrix_sha,
            "confirmation_sha": request.confirmation_sha,
            "test_readback_receipt_sha": request.test_readback_receipt_sha,
            "issued_at": issued_at,
            "expires_at": request.expires_at,
            "nonce": request.nonce,
            "approver_id": principal_id,
            "approver_role": principal_role,
        }
        grant = self._sign_grant(PausedCreateGrant, unsigned)
        stored = self.store.issue_grant(
            grant_id=grant.grant_id,
            project_id=project_id,
            grant_type="PRODUCTION_CREATE",
            payload=grant.model_dump(mode="json", exclude_none=True),
            expires_at=grant.expires_at,
        )
        current = project["execution_state"]
        if current in {
            ExecutionState.NONE.value,
            ExecutionState.RECOVERY_REQUIRED.value,
        }:
            self.store.transition_execution(
                project_id,
                ExecutionState.PRODUCTION_CREATE_AUTHORIZED,
                actor_id=principal_id,
                reason_code="PRODUCTION_PAUSED_CREATE_GRANT_ISSUED",
            )
        else:
            self.store.revoke_grant(
                grant.grant_id,
                actor_id="system",
                reason=f"execution state {current} cannot accept production create",
            )
            raise SeedingError(
                "EXECUTION_STATE_CONFLICT",
                "execution state cannot accept production create",
                status_code=409,
            )
        return stored

    def enqueue_production_create(
        self,
        project_id: str,
        request: ExecuteRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(
            project_id, principal_id, write=True, required_access="EXECUTE"
        )
        if self.production_create_engine is None:
            raise SeedingError(
                "PRODUCTION_CREATE_ADAPTER_NOT_CONFIGURED",
                "production Juguang and account relock adapters are not configured",
                status_code=503,
            )
        stored = self.get_grant(project_id, request.grant_id, principal_id=principal_id)
        grant = PausedCreateGrant.model_validate(stored["payload"])
        if (
            request.grant_sha256 != grant.grant_sha256
            or request.signature_sha256 != grant.signature_sha256
        ):
            raise SeedingError(
                "GRANT_INTEGRITY_MISMATCH",
                "grant proof does not match",
                status_code=409,
            )
        self._verify_execution_grant_signature(grant)
        advertiser = project["config"]["advertiser"]
        if grant.advertiser_id != advertiser["advertiser_id"]:
            raise SeedingError(
                "GRANT_SCOPE_MISMATCH", "grant advertiser is stale", status_code=409
            )
        plans = self._execution_plans_for_grant(project_id, grant)
        job_payload = {
            "grant_id": grant.grant_id,
            "grant_sha256": grant.grant_sha256,
            "advertiser_id": grant.advertiser_id,
            "environment": "PRODUCTION",
            "plans": plans,
        }
        existing = self.store.find_job(
            project_id=project_id,
            kind="PRODUCTION_CREATE",
            input_sha=sha256_json(job_payload),
        )
        if existing is not None:
            return existing
        if (
            project["execution_state"]
            != ExecutionState.PRODUCTION_CREATE_AUTHORIZED.value
        ):
            raise SeedingError(
                "EXECUTION_STATE_CONFLICT",
                "production creation is not authorized",
                status_code=409,
            )
        job = self.store.consume_grant_and_enqueue_job(
            grant_id=grant.grant_id,
            expected_grant_type="PRODUCTION_CREATE",
            project_id=project_id,
            kind="PRODUCTION_CREATE",
            payload=job_payload,
            max_attempts=1,
        )
        self.store.transition_execution(
            project_id,
            ExecutionState.WRITE_IN_PROGRESS,
            actor_id=principal_id,
            reason_code="PRODUCTION_CREATE_GRANT_CONSUMED",
        )
        return job

    def _process_production_create(self, job: Dict[str, Any]) -> Dict[str, Any]:
        if self.production_create_engine is None:
            raise SeedingError(
                "PRODUCTION_CREATE_ADAPTER_NOT_CONFIGURED",
                "production adapter is unavailable",
                status_code=503,
            )
        payload = job["input"]
        plans = tuple(ExecutionPlanInput(**item) for item in payload["plans"])
        receipt = self.production_create_engine.execute(
            advertiser_id=int(payload["advertiser_id"]), plans=plans
        )
        result = asdict(receipt)
        self.store.record_execution_result(
            project_id=job["project_id"], grant_id=payload["grant_id"], result=result
        )
        self.store.put_artifact(
            project_id=job["project_id"],
            kind="production_paused_readback",
            input_sha=job["input_sha"],
            content=result,
        )
        target_by_status = {
            "VERIFIED_PAUSED": ExecutionState.WAITING_RELEASE_AUTHORIZATION,
            "RECONCILE_REQUIRED": ExecutionState.RECONCILE_REQUIRED,
            "PAUSE_FAILED_EMERGENCY": ExecutionState.PAUSE_FAILED_EMERGENCY,
            "READBACK_MISMATCH": ExecutionState.READBACK_MISMATCH,
            "RELOCK_FAILED": ExecutionState.RELOCK_FAILED,
            "FAILED_TERMINAL": ExecutionState.RECOVERY_REQUIRED,
        }
        self.store.transition_execution(
            job["project_id"],
            target_by_status.get(receipt.status, ExecutionState.RECOVERY_REQUIRED),
            actor_id="durable-worker",
            reason_code=f"PRODUCTION_CREATE_{receipt.status}",
        )
        return result

    def issue_release_grant(
        self,
        project_id: str,
        request: IssueReleaseGrantRequest,
        *,
        principal_id: str,
        principal_role: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(
            project_id, principal_id, write=True, required_access="APPROVE"
        )
        if self.grant_signer is None:
            raise SeedingError(
                "GRANT_SIGNER_NOT_CONFIGURED",
                "grant signer is not configured",
                status_code=503,
            )
        advertiser = project["config"]["advertiser"]
        if advertiser["environment"] != "PRODUCTION":
            raise SeedingError(
                "ENVIRONMENT_SCOPE_MISMATCH",
                "release requires a PRODUCTION advertiser",
                status_code=409,
            )
        if (
            project["execution_state"]
            != ExecutionState.WAITING_RELEASE_AUTHORIZATION.value
        ):
            raise SeedingError(
                "EXECUTION_STATE_CONFLICT",
                "paused production readback is required before release",
                status_code=409,
            )
        matrix = self.store.latest_artifact(project_id, "plan_matrix")
        confirmation = self.store.latest_artifact(project_id, "confirmation")
        readback = self.store.latest_artifact(project_id, "production_paused_readback")
        if (
            request.config_sha != project["config_sha"]
            or request.matrix_sha != matrix["content_sha"]
            or request.confirmation_sha != confirmation["content_sha"]
            or request.readback_receipt_sha != readback["content_sha"]
        ):
            raise SeedingError(
                "RELEASE_PROOF_MISMATCH", "release proofs are stale", status_code=409
            )
        active = sorted(
            self.store.list_active_plans(project_id),
            key=lambda item: item["plan_revision_id"],
        )
        expected_plan_ids = tuple(item["plan_revision_id"] for item in active)
        expected_payload_sha = sha256_json(
            tuple(item["payload"]["platform_payload_sha256"] for item in active)
        )
        receipt_by_plan = {
            item["plan_revision_id"]: item
            for item in readback["content"]["plan_receipts"]
        }
        try:
            expected_bindings = tuple(
                ReleaseObjectBinding(
                    plan_revision_id=plan_id,
                    campaign_id=int(
                        receipt_by_plan[plan_id]["object_ids"]["campaign_id"]
                    ),
                    unit_id=int(receipt_by_plan[plan_id]["object_ids"]["unit_id"]),
                    creativity_ids=tuple(
                        int(value)
                        for value in receipt_by_plan[plan_id]["object_ids"][
                            "creativity_ids"
                        ]
                    ),
                )
                for plan_id in expected_plan_ids
            )
            expected_campaign_ids = tuple(
                item.campaign_id for item in expected_bindings
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SeedingError(
                "PRODUCTION_READBACK_INCOMPLETE",
                "production readback does not contain exact campaign/unit/creative ids",
                status_code=409,
            ) from exc
        if (
            request.plan_revision_ids != expected_plan_ids
            or request.platform_campaign_ids != expected_campaign_ids
            or request.platform_object_bindings != expected_bindings
            or request.payload_sha != expected_payload_sha
        ):
            raise SeedingError(
                "RELEASE_OBJECT_MISMATCH",
                "release objects are not the verified paused objects",
                status_code=409,
            )
        now = datetime.now(timezone.utc)
        if request.expires_at <= now or request.expires_at <= request.start_at:
            raise SeedingError(
                "RELEASE_WINDOW_INVALID",
                "release time window is invalid",
                status_code=422,
            )
        unsigned = {
            "release_grant_id": "releasegrant_" + uuid.uuid4().hex,
            "project_id": project_id,
            "environment": "PRODUCTION",
            "platform": "XIAOHONGSHU_JUGUANG",
            "advertiser_id": advertiser["advertiser_id"],
            "plan_revision_ids": request.plan_revision_ids,
            "platform_campaign_ids": request.platform_campaign_ids,
            "platform_object_bindings": request.platform_object_bindings,
            "account_id": advertiser["account_id"],
            "authorization_domain_id": advertiser["authorization_domain_id"],
            "allowed_actions": ("ENABLE", "PAUSE"),
            "config_sha": request.config_sha,
            "matrix_sha": request.matrix_sha,
            "confirmation_sha": request.confirmation_sha,
            "payload_sha": request.payload_sha,
            "readback_receipt_sha": request.readback_receipt_sha,
            "start_at": request.start_at,
            "spend_cap_fen": request.spend_cap_fen,
            "spend_cap_period": request.spend_cap_period,
            "spend_monitor_source": request.spend_monitor_source,
            "spend_monitor_proof_sha256": request.spend_monitor_proof_sha256,
            "exceed_action": "PAUSE_AND_RELOCK",
            "issued_at": now,
            "expires_at": request.expires_at,
            "nonce": request.nonce,
            "approver_id": principal_id,
            "approver_role": principal_role,
        }
        grant = self._sign_grant(ReleaseGrant, unsigned)
        return self.store.issue_grant(
            grant_id=grant.release_grant_id,
            project_id=project_id,
            grant_type="RELEASE",
            payload=grant.model_dump(mode="json", exclude_none=True),
            expires_at=grant.expires_at,
        )

    def enqueue_release(
        self,
        project_id: str,
        release_grant_id: str,
        request: ReleaseExecuteRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        self.authorize_project(
            project_id, principal_id, write=True, required_access="EXECUTE"
        )
        if self.release_engine is None:
            raise SeedingError(
                "RELEASE_ADAPTER_NOT_CONFIGURED",
                "release safety adapter is not configured",
                status_code=503,
            )
        row = self.get_grant(project_id, release_grant_id, principal_id=principal_id)
        grant = ReleaseGrant.model_validate(row["payload"])
        if (
            request.grant_sha256 != grant.grant_sha256
            or request.signature_sha256 != grant.signature_sha256
        ):
            raise SeedingError(
                "GRANT_INTEGRITY_MISMATCH",
                "release grant proof does not match",
                status_code=409,
            )
        self._verify_execution_grant_signature(grant)
        now = datetime.now(timezone.utc)
        if now < grant.start_at or now >= grant.expires_at:
            raise SeedingError(
                "RELEASE_WINDOW_CLOSED",
                "release grant is outside its activation window",
                status_code=409,
            )
        payload = {
            "grant_id": release_grant_id,
            "advertiser_id": grant.advertiser_id,
            "campaign_ids": grant.platform_campaign_ids,
            "spend_cap_fen": grant.spend_cap_fen,
            "spend_cap_period": grant.spend_cap_period,
            "monitor_source": grant.spend_monitor_source,
        }
        return self.store.consume_grant_and_enqueue_job(
            grant_id=release_grant_id,
            expected_grant_type="RELEASE",
            project_id=project_id,
            kind="RELEASE",
            payload=payload,
            max_attempts=1,
        )

    def _process_release(self, job: Dict[str, Any]) -> Dict[str, Any]:
        if self.release_engine is None:
            raise SeedingError(
                "RELEASE_ADAPTER_NOT_CONFIGURED",
                "release adapter is unavailable",
                status_code=503,
            )
        data = job["input"]
        result = asdict(
            self.release_engine.activate(
                advertiser_id=int(data["advertiser_id"]),
                campaign_ids=tuple(data["campaign_ids"]),
                spend_cap_fen=int(data["spend_cap_fen"]),
                spend_cap_period=data["spend_cap_period"],
                monitor_source=data["monitor_source"],
            )
        )
        self.store.put_artifact(
            project_id=job["project_id"],
            kind="release_activation",
            input_sha=job["input_sha"],
            content=result,
        )
        if result["status"] == "RELEASE_ACTIVE":
            self.store.transition_delivery(
                job["project_id"],
                DeliveryState.FEED_LEARNING,
                actor_id="durable-worker",
                reason_code="RELEASE_ACTIVE_WITH_SPEND_CAP",
            )
        return result

    def enqueue_release_evaluation(
        self,
        project_id: str,
        release_grant_id: str,
        request: ReleaseEvaluationRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(
            project_id, principal_id, write=True, required_access="EXECUTE"
        )
        if self.release_engine is None:
            raise SeedingError(
                "RELEASE_ADAPTER_NOT_CONFIGURED",
                "release safety adapter is not configured",
                status_code=503,
            )
        row = self.get_grant(project_id, release_grant_id, principal_id=principal_id)
        grant = ReleaseGrant.model_validate(row["payload"])
        if (
            request.grant_sha256 != grant.grant_sha256
            or request.signature_sha256 != grant.signature_sha256
        ):
            raise SeedingError(
                "GRANT_INTEGRITY_MISMATCH",
                "release grant proof does not match",
                status_code=409,
            )
        self._verify_execution_grant_signature(grant)
        if project["delivery_state"] not in {
            DeliveryState.FEED_LEARNING.value,
            DeliveryState.SEARCH_ELIGIBLE.value,
            DeliveryState.STABLE.value,
        }:
            raise SeedingError(
                "DELIVERY_NOT_ACTIVE",
                "there is no active release to evaluate",
                status_code=409,
            )
        return self.store.enqueue_job(
            project_id=project_id,
            kind="RELEASE_EVALUATE",
            payload={
                "evaluation_key": request.evaluation_key,
                "grant_id": release_grant_id,
                "advertiser_id": grant.advertiser_id,
                "campaign_ids": grant.platform_campaign_ids,
                "spend_cap_fen": grant.spend_cap_fen,
                "spend_cap_period": grant.spend_cap_period,
            },
            max_attempts=1,
        )

    def _process_release_evaluation(self, job: Dict[str, Any]) -> Dict[str, Any]:
        if self.release_engine is None:
            raise SeedingError(
                "RELEASE_ADAPTER_NOT_CONFIGURED",
                "release adapter is unavailable",
                status_code=503,
            )
        data = job["input"]
        result = asdict(
            self.release_engine.evaluate_spend(
                advertiser_id=int(data["advertiser_id"]),
                campaign_ids=tuple(data["campaign_ids"]),
                spend_cap_fen=int(data["spend_cap_fen"]),
                spend_cap_period=data["spend_cap_period"],
            )
        )
        self.store.put_artifact(
            project_id=job["project_id"],
            kind="release_spend_evaluation",
            input_sha=job["input_sha"],
            content=result,
        )
        if result["status"].startswith("CAP_EXCEEDED"):
            self.store.transition_delivery(
                job["project_id"],
                DeliveryState.CLOSING,
                actor_id="durable-worker",
                reason_code=result["status"],
            )
            if result["status"] == "CAP_EXCEEDED_PAUSED":
                self.store.transition_delivery(
                    job["project_id"],
                    DeliveryState.CLOSED,
                    actor_id="durable-worker",
                    reason_code="SPEND_CAP_PAUSED_AND_RELOCKED",
                )
        return result

    def _process_release_recovery(self, job: Dict[str, Any]) -> Dict[str, Any]:
        if self.release_engine is None:
            raise SeedingError(
                "RELEASE_ADAPTER_NOT_CONFIGURED",
                "release adapter is unavailable",
                status_code=503,
            )
        data = job["input"]["release_input"]
        receipt_id, safe = self.release_engine.pause_and_relock(
            advertiser_id=int(data["advertiser_id"]),
            campaign_ids=tuple(data["campaign_ids"]),
        )
        result = {
            "status": "RELEASE_RECOVERED_SAFE" if safe else "RELEASE_RECOVERY_FAILED",
            "advertiser_id": int(data["advertiser_id"]),
            "campaign_ids": list(data["campaign_ids"]),
            "emergency_relock_receipt_id": receipt_id,
        }
        self.store.put_artifact(
            project_id=job["project_id"],
            kind="release_recovery",
            input_sha=job["input_sha"],
            content=result,
        )
        project = self.store.get_project(job["project_id"])
        delivery = project["delivery_state"]
        if safe:
            if delivery == DeliveryState.NOT_RELEASED.value:
                self.store.transition_delivery(
                    job["project_id"],
                    DeliveryState.CLOSED,
                    actor_id="durable-worker",
                    reason_code="RELEASE_RECOVERED_BEFORE_VERIFIED_ACTIVATION",
                )
            elif delivery in {
                DeliveryState.FEED_LEARNING.value,
                DeliveryState.SEARCH_ELIGIBLE.value,
                DeliveryState.STABLE.value,
            }:
                self.store.transition_delivery(
                    job["project_id"],
                    DeliveryState.CLOSING,
                    actor_id="durable-worker",
                    reason_code="RELEASE_RECOVERY_STARTED",
                )
                self.store.transition_delivery(
                    job["project_id"],
                    DeliveryState.CLOSED,
                    actor_id="durable-worker",
                    reason_code="RELEASE_RECOVERED_SAFE",
                )
        elif delivery in {
            DeliveryState.FEED_LEARNING.value,
            DeliveryState.SEARCH_ELIGIBLE.value,
            DeliveryState.STABLE.value,
        }:
            self.store.transition_delivery(
                job["project_id"],
                DeliveryState.CLOSING,
                actor_id="durable-worker",
                reason_code="RELEASE_RECOVERY_FAILED",
            )
        return result

    def _validated_compiled_scope(
        self,
        *,
        project_id: str,
        project: Dict[str, Any],
        config_sha: str,
        matrix_sha: str,
        confirmation_sha: str,
        object_hashes: Tuple[str, ...],
        plan_revision_ids: Tuple[str, ...],
        platform_payload_hashes: Tuple[str, ...],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
        matrix = self.store.latest_artifact(project_id, "plan_matrix")
        confirmation = self.store.latest_artifact(project_id, "confirmation")
        plans = sorted(
            self.store.list_active_plans(project_id),
            key=lambda item: item["plan_revision_id"],
        )
        if (
            config_sha != project["config_sha"]
            or matrix_sha != matrix["content_sha"]
            or confirmation_sha != confirmation["content_sha"]
        ):
            raise SeedingError(
                "GRANT_SCOPE_MISMATCH", "grant proofs are stale", status_code=409
            )
        expected_ids = tuple(item["plan_revision_id"] for item in plans)
        expected_objects = tuple(sorted(sha256_json(item["payload"]) for item in plans))
        expected_platform = tuple(
            item["payload"].get("platform_payload_sha256", "") for item in plans
        )
        if (
            plan_revision_ids != expected_ids
            or tuple(sorted(object_hashes)) != expected_objects
            or platform_payload_hashes != expected_platform
            or any(not value for value in expected_platform)
        ):
            raise SeedingError(
                "GRANT_OBJECT_MISMATCH",
                "grant objects are stale or uncompiled",
                status_code=409,
            )
        return plans, matrix, confirmation

    def _execution_plans_for_grant(
        self, project_id: str, grant: Any
    ) -> List[Dict[str, Any]]:
        active = {
            item["plan_revision_id"]: item
            for item in self.store.list_active_plans(project_id)
        }
        output: List[Dict[str, Any]] = []
        for plan_id, payload_sha in zip(
            grant.plan_revision_ids, grant.platform_payload_hashes
        ):
            row = active.get(plan_id)
            payload = row["payload"].get("platform_payload") if row else None
            if row is None or payload is None or sha256_json(payload) != payload_sha:
                raise SeedingError(
                    "PLATFORM_PAYLOAD_SHA_MISMATCH",
                    "authorized platform payload changed",
                    status_code=409,
                )
            output.append(
                {
                    "plan_revision_id": plan_id,
                    "logical_plan_key": row["logical_plan_key"],
                    "payload": payload,
                    "payload_sha256": payload_sha,
                }
            )
        return output

    def _sign_grant(self, model_class: Any, unsigned: Dict[str, Any]) -> Any:
        if self.grant_signer is None:
            raise SeedingError(
                "GRANT_SIGNER_NOT_CONFIGURED",
                "grant signer is not configured",
                status_code=503,
            )
        normalized = model_class.model_validate(
            {**unsigned, "grant_sha256": "0" * 64, "signature_sha256": "0" * 64}
        ).model_dump(
            mode="json",
            exclude_none=True,
            exclude={"grant_sha256", "signature_sha256"},
        )
        grant_sha = sha256_json(normalized)
        signed = {**normalized, "grant_sha256": grant_sha}
        return model_class.model_validate(
            {**signed, "signature_sha256": self.grant_signer.sign(signed)}
        )

    def _process_prepare(self, job: Dict[str, Any]) -> Dict[str, Any]:
        request = PrepareRequest.model_validate(job["input"])
        project_id = job["project_id"]
        project = self.store.get_project(project_id)
        if request.config_sha != project["config_sha"]:
            raise SeedingError(
                "CONFIG_SHA_MISMATCH",
                "project changed after job enqueue",
                status_code=409,
            )

        self._validate_parameter_set(project, request)

        self.store.put_artifact(
            project_id=project_id,
            kind="prepare_input",
            input_sha=job["input_sha"],
            content=request.model_dump(mode="json", by_alias=True),
        )
        return self._continue_prepare_after_validation(
            job=job, request=request, project=project
        )

    @staticmethod
    def _validate_parameter_set(
        project: Dict[str, Any], request: PrepareRequest
    ) -> None:
        project_config = project["config"]
        if (
            request.parameter_set.parameter_set_id != project_config["parameter_set_id"]
            or request.parameter_set.version != project_config["parameter_set_version"]
        ):
            raise SeedingError(
                "PARAMETER_SET_MISMATCH",
                "prepare parameter set does not match ProjectConfig",
                status_code=409,
                details={
                    "expected_id": project_config["parameter_set_id"],
                    "expected_version": project_config["parameter_set_version"],
                },
            )

    def _continue_prepare_after_validation(
        self,
        *,
        job: Dict[str, Any],
        request: PrepareRequest,
        project: Dict[str, Any],
    ) -> Dict[str, Any]:
        project_id = job["project_id"]
        current = self.store.get_project(project_id)["project_state"]
        if current in {
            ProjectState.ADVISORY_READY.value,
            ProjectState.FAILED_CLOSED.value,
            ProjectState.WAITING_BUDGET.value,
        }:
            self.store.transition_project(
                project_id,
                ProjectState.INPUT_VALIDATED,
                actor_id="durable-worker",
                reason_code="NEW_PREPARE_REVISION",
            )
        self.store.transition_project(
            project_id,
            ProjectState.THREE_CHAIN_READY,
            actor_id="durable-worker",
            reason_code="THREE_CHAIN_INPUTS_HASHED",
        )

        eligible_notes = {
            note.note_id: note
            for note in request.notes
            if note.eligibility and note.status == "AVAILABLE"
        }
        audience_decisions = evaluate_audiences(
            request.audiences,
            parameters=request.parameter_set,
            overlap_evidence=request.overlap_evidence,
        )
        eligible_audiences = {
            audience.audience_id: audience
            for audience in request.audiences
            if audience.audience_id in audience_decisions.eligible_audience_ids
        }
        if not eligible_notes or not eligible_audiences:
            raise SeedingError(
                "SEEDING_CONFIG_REVIEW_REQUIRED",
                "prepare requires at least one eligible note and audience",
            )

        pair_rows = self._eligible_pairs(
            request.pairs,
            note_ids=set(eligible_notes),
            audience_ids=set(eligible_audiences),
            min_pair_score=request.min_pair_score,
        )
        if len(pair_rows) > MAX_PAIR_COMBINATIONS:
            raise SeedingError(
                "COMBINATION_LIMIT_EXCEEDED",
                "pair candidate limit exceeded",
                details={"limit": MAX_PAIR_COMBINATIONS, "actual": len(pair_rows)},
            )

        per_note_eligible = min(
            len({pair.asset_id for pair in pair_rows if pair.note_id == note_id})
            for note_id in eligible_notes
        )
        if per_note_eligible <= 0:
            raise SeedingError(
                "PAIRING_COVERAGE_INSUFFICIENT",
                "every eligible note requires at least one evidence-backed audience pair",
            )
        capacity = uniform_budget_capacity(
            expected_daily_spend_fen=request.budget.expected_daily_spend_fen,
            observation_days=request.budget.observation_days,
            phase_budget_fen=request.budget.phase_budget_fen,
            note_count=len(eligible_notes),
            eligible_audiences_per_note=per_note_eligible,
            business_cap=request.budget.business_audience_cap,
        )
        if capacity.waiting_budget:
            preview = {
                "schema": "seeding.preview.v1",
                "project_id": project_id,
                "status": "WAITING_BUDGET",
                "capacity": capacity.__dict__,
                "plans": [],
                "risks": ["BUDGET_INSUFFICIENT"],
                "platform_business_write_count": 0,
            }
            artifact = self.store.put_artifact(
                project_id=project_id,
                kind="plan_matrix",
                input_sha=job["input_sha"],
                content=preview,
            )
            self.store.transition_project(
                project_id,
                ProjectState.WAITING_BUDGET,
                actor_id="durable-worker",
                reason_code="BUDGET_CAPACITY_ZERO",
            )
            return artifact

        selected = self._select_pairs(pair_rows, capacity.selected_audiences_per_note)
        config: Dict[str, Any] = project["config"]
        plans: List[Dict[str, Any]] = []
        for pair in selected:
            logical_key = logical_plan_key(
                account_id=config["advertiser"]["account_id"],
                project_id=project_id,
                stage=request.stage,
                channel="FEED",
                note_id=pair.note_id,
                objective=request.objective,
                audience_id=pair.asset_id,
            )
            plan = FeedPlanCandidate(
                note_id=pair.note_id,
                audience_id=pair.asset_id,
                objective=request.objective,
                daily_budget_fen=request.budget.platform_daily_budget_fen,
                expected_daily_spend_fen=request.budget.expected_daily_spend_fen,
                identity=PlanIdentity(
                    logical_plan_key=logical_key,
                    plan_revision_id="planrev_" + uuid.uuid4().hex,
                    stage=request.stage,
                    revision_status="ACTIVE",
                ),
            ).model_dump(mode="json")
            plans.append(
                {
                    **plan,
                    "channel": "FEED",
                    "asset_identity": pair.asset_id,
                }
            )

        keyword_decisions = None
        search_gate_receipts: Dict[str, Dict[str, Any]] = {}
        if request.keywords:
            keyword_decisions = evaluate_keywords(
                request.keywords,
                parameters=request.parameter_set,
                semantic_evidence=request.semantic_duplicate_evidence,
                bid_mode=request.bid_mode,
            )
        if config["search"]["enabled"] and keyword_decisions is not None:
            release_by_note = {
                item.note_id: item for item in request.search_release_evidence
            }
            lane_pairs = self._eligible_keyword_lane_pairs(
                request.pairs,
                note_ids=set(eligible_notes),
                min_pair_score=request.min_pair_score,
            )
            for pair in lane_pairs:
                evidence = release_by_note.get(pair.note_id)
                if evidence is None:
                    search_gate_receipts[pair.note_id] = {
                        "allowed": False,
                        "reason_codes": ["SEARCH_RELEASE_EVIDENCE_MISSING"],
                    }
                    continue
                allowed, gate_reasons = search_release_allowed(
                    evidence, parameters=request.parameter_set
                )
                search_gate_receipts[pair.note_id] = {
                    "allowed": allowed,
                    "reason_codes": list(gate_reasons),
                    "source_proof_sha256": evidence.source_proof_sha256,
                }
                if not allowed:
                    continue
                lane = pair.asset_id
                keyword_ids = keyword_decisions.eligible_by_lane.get(lane, tuple())
                if not keyword_ids:
                    continue
                logical_key = logical_plan_key(
                    account_id=config["advertiser"]["account_id"],
                    project_id=project_id,
                    stage=request.stage,
                    channel="SEARCH",
                    note_id=pair.note_id,
                    objective=request.objective,
                    primary_lane=lane,
                )
                plan = SearchPlanCandidate(
                    note_id=pair.note_id,
                    primary_lane=lane,
                    keyword_ids=keyword_ids,
                    bid_mode=request.bid_mode,
                    daily_budget_fen=request.budget.platform_daily_budget_fen,
                    expected_daily_spend_fen=request.budget.expected_daily_spend_fen,
                    release_evidence=search_gate_receipts[pair.note_id],
                    identity=PlanIdentity(
                        logical_plan_key=logical_key,
                        plan_revision_id="planrev_" + uuid.uuid4().hex,
                        stage=request.stage,
                        revision_status="ACTIVE",
                    ),
                ).model_dump(mode="json")
                plans.append(
                    {
                        **plan,
                        "channel": "SEARCH",
                        "objective": request.objective,
                        "asset_identity": lane,
                    }
                )

        profile = request.parameter_set.juguang_profile
        if profile is not None and profile.verified:
            project_contract = ProjectConfig.model_validate(config)
            audiences_by_id = {item.audience_id: item for item in request.audiences}
            keywords_by_id = {item.keyword_id: item for item in request.keywords}
            keyword_receipts = (
                {item.keyword_id: item for item in keyword_decisions.decisions}
                if keyword_decisions is not None
                else {}
            )
            compiled_plans: List[Dict[str, Any]] = []
            for plan in plans:
                if plan["channel"] == "FEED":
                    receipt = compile_feed_plan(
                        project=project_contract,
                        plan=plan,
                        audience=audiences_by_id[plan["audience_id"]],
                        keywords_by_id=keywords_by_id,
                        profile=profile,
                    )
                else:
                    receipt = compile_search_plan(
                        project=project_contract,
                        plan=plan,
                        keywords_by_id=keywords_by_id,
                        keyword_decisions=keyword_receipts,
                        profile=profile,
                    )
                compiled_plans.append(
                    {
                        **plan,
                        "platform_payload": receipt.payload.model_dump(
                            mode="json", exclude_none=True
                        ),
                        "platform_payload_sha256": receipt.payload_sha256,
                        "platform_profile_id": receipt.platform_profile_id,
                        "platform_profile_version": receipt.platform_profile_version,
                        "platform_verification_proof_sha256": (
                            receipt.verification_proof_sha256
                        ),
                    }
                )
            plans = compiled_plans

        risk_set = (
            set(self._risks(project, request))
            | set(audience_decisions.risks)
            | set(keyword_decisions.risks if keyword_decisions else tuple())
        )
        if plans and all("platform_payload" in plan for plan in plans):
            risk_set.discard("PLATFORM_ENUM_UNVERIFIED")

        preview = {
            "schema": "seeding.preview.v1",
            "project_id": project_id,
            "status": "WAITING_CONFIRMATION",
            "capacity": capacity.__dict__,
            "plans": plans,
            "audience_decisions": [
                item.__dict__ for item in audience_decisions.decisions
            ],
            "keyword_decisions": (
                [item.__dict__ for item in keyword_decisions.decisions]
                if keyword_decisions is not None
                else []
            ),
            "search_gate_receipts": search_gate_receipts,
            "risks": sorted(risk_set),
            "platform_business_write_count": 0,
        }
        matrix_sha = sha256_json(preview)
        artifact = self.store.put_artifact(
            project_id=project_id,
            kind="plan_matrix",
            input_sha=job["input_sha"],
            content=preview,
        )
        self.store.replace_active_plan_revisions(
            project_id=project_id,
            stage=request.stage,
            config_sha=request.config_sha,
            matrix_sha=matrix_sha,
            parameter_set_version=config["parameter_set_version"],
            plans=plans,
        )
        self.store.transition_project(
            project_id,
            ProjectState.MATRIX_READY,
            actor_id="durable-worker",
            reason_code="PLAN_MATRIX_COMPILED",
        )
        self.store.transition_project(
            project_id,
            ProjectState.WAITING_CONFIRMATION,
            actor_id="durable-worker",
            reason_code="BUSINESS_CONFIRMATION_REQUIRED",
        )
        return artifact

    @staticmethod
    def _eligible_pairs(
        pairs: Tuple[PairCandidate, ...],
        *,
        note_ids: Set[str],
        audience_ids: Set[str],
        min_pair_score: float,
    ) -> List[PairCandidate]:
        return [
            pair
            for pair in pairs
            if pair.pair_type == "NOTE_AUDIENCE"
            and pair.note_id in note_ids
            and pair.asset_id in audience_ids
            and pair.pair_score >= min_pair_score
            and pair.match_grade != "REVIEW"
        ]

    @staticmethod
    def _eligible_keyword_lane_pairs(
        pairs: Tuple[PairCandidate, ...],
        *,
        note_ids: Set[str],
        min_pair_score: float,
    ) -> List[PairCandidate]:
        by_note_lane: Dict[Tuple[str, str], PairCandidate] = {}
        for pair in pairs:
            if (
                pair.pair_type != "NOTE_KEYWORD_LANE"
                or pair.note_id not in note_ids
                or pair.pair_score < min_pair_score
                or pair.match_grade == "REVIEW"
            ):
                continue
            key = (pair.note_id, pair.asset_id)
            existing = by_note_lane.get(key)
            if existing is None or pair.pair_score > existing.pair_score:
                by_note_lane[key] = pair
        return [by_note_lane[key] for key in sorted(by_note_lane)]

    @staticmethod
    def _select_pairs(pairs: List[PairCandidate], per_note: int) -> List[PairCandidate]:
        by_note: Dict[str, List[PairCandidate]] = {}
        for pair in pairs:
            by_note.setdefault(pair.note_id, []).append(pair)
        selected: List[PairCandidate] = []
        for note_id in sorted(by_note):
            ranked = sorted(
                by_note[note_id], key=lambda item: (-item.pair_score, item.asset_id)
            )
            seen: Set[str] = set()
            for pair in ranked:
                if pair.asset_id in seen:
                    continue
                seen.add(pair.asset_id)
                selected.append(pair)
                if len(seen) == per_note:
                    break
        return selected

    @staticmethod
    def _risks(project: Dict[str, Any], request: PrepareRequest) -> List[str]:
        risks = []
        if any(
            audience.audience_mode == "DYNAMIC_BEHAVIOR"
            for audience in request.audiences
        ):
            risks.append("DYNAMIC_BEHAVIOR_NOT_STATIC_SCORED")
        risks.append("PLATFORM_ENUM_UNVERIFIED")
        return risks

    def preview(self, project_id: str, *, principal_id: str) -> Dict[str, Any]:
        project = self.authorize_project(project_id, principal_id, write=False)
        return {
            "project": project,
            "artifacts": self.store.list_artifacts(project_id),
            "active_plans": self.store.list_active_plans(project_id),
            "jobs": self.store.list_jobs(project_id),
            "platform_business_write_count": self.store.count_execution_attempts(
                project_id
            ),
        }

    def confirm(
        self,
        project_id: str,
        request: ConfirmRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(
            project_id, principal_id, write=True, required_access="APPROVE"
        )
        if project["config_sha"] != request.config_sha:
            raise SeedingError(
                "CONFIG_SHA_MISMATCH",
                "config changed before confirmation",
                status_code=409,
            )
        matrix = self.store.latest_artifact(project_id, "plan_matrix")
        if matrix["content_sha"] != request.matrix_sha:
            raise SeedingError(
                "MATRIX_SHA_MISMATCH",
                "matrix changed before confirmation",
                status_code=409,
            )
        active_hashes = tuple(
            sorted(
                sha256_json(item["payload"])
                for item in self.store.list_active_plans(project_id)
            )
        )
        if tuple(sorted(request.object_hashes)) != active_hashes:
            raise SeedingError(
                "OBJECT_HASH_MISMATCH",
                "confirmed objects do not match active plans",
                status_code=409,
            )
        confirmation = self.store.put_artifact(
            project_id=project_id,
            kind="confirmation",
            input_sha=sha256_json(request),
            content=request.model_dump(mode="json"),
        )
        self.store.transition_project(
            project_id,
            ProjectState.ADVISORY_READY,
            actor_id=principal_id,
            reason_code="BUSINESS_OBJECTS_CONFIRMED",
        )
        return confirmation

    @staticmethod
    def locked_execution() -> None:
        raise ExecutionLocked()
