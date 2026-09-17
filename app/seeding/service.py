"""Application service for the read-only SEEDING Phase 0/1 slice."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Set, Tuple

from .algorithms import uniform_budget_capacity
from .contracts import (
    ConfirmRequest,
    FeedPlanCandidate,
    PairCandidate,
    PlanIdentity,
    PrepareRequest,
    ProjectConfig,
)
from .errors import ExecutionLocked, SeedingError
from .identity import logical_plan_key, sha256_json
from .state import ProjectState
from .store import SeedingStore

MAX_PAIR_COMBINATIONS = 200_000


class SeedingService:
    def __init__(self, store: SeedingStore) -> None:
        self.store = store

    def create_project(
        self, config: ProjectConfig, *, principal_id: str
    ) -> Dict[str, Any]:
        return self.store.create_project(config, owner_principal_id=principal_id)

    def authorize_project(
        self, project_id: str, principal_id: str, *, write: bool
    ) -> Dict[str, Any]:
        project = self.store.get_project(project_id)
        if project["owner_principal_id"] != principal_id:
            raise SeedingError(
                "RBAC_FORBIDDEN", "project access denied", status_code=403
            )
        return project

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
            if job["kind"] != "PREPARE":
                raise SeedingError("UNKNOWN_JOB_KIND", "unknown durable job kind")
            result = self._process_prepare(job)
            self.store.complete_job(job["job_id"], worker_id=worker_id)
            return result
        except Exception as exc:
            self.store.fail_job(
                job["job_id"], worker_id=worker_id, error=str(exc)[:2000]
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

        self.store.put_artifact(
            project_id=project_id,
            kind="prepare_input",
            input_sha=job["input_sha"],
            content=request.model_dump(mode="json", by_alias=True),
        )
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
        eligible_audiences = {
            audience.audience_id: audience
            for audience in request.audiences
            if audience.metric_state == "READY" and all(audience.hard_gates.values())
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

        preview = {
            "schema": "seeding.preview.v1",
            "project_id": project_id,
            "status": "WAITING_CONFIRMATION",
            "capacity": capacity.__dict__,
            "plans": plans,
            "risks": self._risks(project, request),
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
        if project["config"]["search"]["enabled"]:
            risks.append("SEARCH_RELEASE_GATE_NOT_EVALUATED")
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
            "platform_business_write_count": 0,
        }

    def confirm(
        self,
        project_id: str,
        request: ConfirmRequest,
        *,
        principal_id: str,
    ) -> Dict[str, Any]:
        project = self.authorize_project(project_id, principal_id, write=True)
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
