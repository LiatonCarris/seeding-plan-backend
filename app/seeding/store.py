"""SQLite authority for SEEDING projects, jobs, artifacts, and outbox."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..sqlite_utils import connect_sqlite
from .contracts import ProjectConfig
from .errors import SeedingError
from .identity import canonical_json, sha256_json
from .state import (
    DeliveryState,
    ExecutionState,
    ProjectState,
    validate_project_transition,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class SeedingStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = connect_sqlite(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def migrate(self) -> None:
        migration = Path(__file__).with_name("migrations") / "001_initial.sql"
        with self.connect() as connection:
            connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (1, iso(utc_now())),
            )

    def create_project(
        self, config: ProjectConfig, *, owner_principal_id: str
    ) -> Dict[str, Any]:
        now = iso(utc_now())
        config_json = canonical_json(config)
        config_sha = sha256_json(config)
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, owner_principal_id, config_json, config_sha,
                        project_state, execution_state, delivery_state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        config.project_id,
                        owner_principal_id,
                        config_json,
                        config_sha,
                        ProjectState.DRAFT.value,
                        ExecutionState.NONE.value,
                        DeliveryState.NOT_RELEASED.value,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO project_revisions(
                        project_id, revision, config_json, config_sha, created_by, created_at
                    ) VALUES (?, 1, ?, ?, ?, ?)
                    """,
                    (
                        config.project_id,
                        config_json,
                        config_sha,
                        owner_principal_id,
                        now,
                    ),
                )
                self._append_outbox(
                    connection,
                    project_id=config.project_id,
                    event_type="PROJECT_CREATED",
                    payload={"config_sha": config_sha, "revision": 1},
                    now=now,
                )
            except sqlite3.IntegrityError as exc:
                raise SeedingError(
                    "PROJECT_ALREADY_EXISTS",
                    "project_id already exists",
                    status_code=409,
                    details={"project_id": config.project_id},
                ) from exc
        return self.get_project(config.project_id)

    def get_project(self, project_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise SeedingError(
                "PROJECT_NOT_FOUND", "project not found", status_code=404
            )
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        return result

    def transition_project(
        self,
        project_id: str,
        target: ProjectState,
        *,
        actor_id: str,
        reason_code: str,
    ) -> Dict[str, Any]:
        now = iso(utc_now())
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT project_state FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise SeedingError(
                    "PROJECT_NOT_FOUND", "project not found", status_code=404
                )
            current = ProjectState(row["project_state"])
            validate_project_transition(current, target)
            connection.execute(
                "UPDATE projects SET project_state = ?, updated_at = ? WHERE project_id = ?",
                (target.value, now, project_id),
            )
            transition_id = "tr_" + uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO state_transitions(
                    transition_id, project_id, state_surface, state_from, state_to,
                    actor_id, reason_code, created_at
                ) VALUES (?, ?, 'PROJECT', ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    project_id,
                    current.value,
                    target.value,
                    actor_id,
                    reason_code,
                    now,
                ),
            )
            self._append_outbox(
                connection,
                project_id=project_id,
                event_type="PROJECT_STATE_CHANGED",
                payload={
                    "transition_id": transition_id,
                    "from": current.value,
                    "to": target.value,
                    "reason_code": reason_code,
                },
                now=now,
            )
        return self.get_project(project_id)

    def put_artifact(
        self,
        *,
        project_id: str,
        kind: str,
        input_sha: str,
        content: Any,
    ) -> Dict[str, Any]:
        content_json = canonical_json(content)
        content_sha = sha256_json(content)
        now = iso(utc_now())
        artifact_id = "artifact_" + uuid.uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO artifacts(
                    artifact_id, project_id, kind, input_sha, content_sha, content_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    kind,
                    input_sha,
                    content_sha,
                    content_json,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE project_id = ? AND kind = ? AND input_sha = ?",
                (project_id, kind, input_sha),
            ).fetchone()
        assert row is not None
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        return result

    def list_artifacts(self, project_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        output: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["content"] = json.loads(item.pop("content_json"))
            output.append(item)
        return output

    def latest_artifact(self, project_id: str, kind: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE project_id = ? AND kind = ?
                ORDER BY created_at DESC, artifact_id DESC LIMIT 1
                """,
                (project_id, kind),
            ).fetchone()
        if row is None:
            raise SeedingError(
                "ARTIFACT_NOT_FOUND",
                "required artifact was not found",
                status_code=404,
                details={"kind": kind},
            )
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        return result

    def replace_active_plan_revisions(
        self,
        *,
        project_id: str,
        stage: str,
        config_sha: str,
        matrix_sha: str,
        parameter_set_version: int,
        plans: List[Dict[str, Any]],
    ) -> None:
        now = iso(utc_now())
        with self.transaction() as connection:
            for plan in plans:
                logical_key = plan["identity"]["logical_plan_key"]
                connection.execute(
                    """
                    INSERT OR IGNORE INTO logical_plans(
                        logical_plan_key, project_id, stage, channel, note_id,
                        objective, asset_identity, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        logical_key,
                        project_id,
                        stage,
                        plan["channel"],
                        plan["note_id"],
                        plan["objective"],
                        plan["asset_identity"],
                        now,
                    ),
                )
                existing = connection.execute(
                    """
                    SELECT plan_revision_id FROM plan_revisions
                    WHERE logical_plan_key = ? AND stage = ? AND revision_status = 'ACTIVE'
                    """,
                    (logical_key, stage),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        "UPDATE plan_revisions SET revision_status = 'SUPERSEDED' WHERE plan_revision_id = ?",
                        (existing["plan_revision_id"],),
                    )
                connection.execute(
                    """
                    INSERT INTO plan_revisions(
                        plan_revision_id, logical_plan_key, stage, config_sha, matrix_sha,
                        parameter_set_version, revision_status,
                        supersedes_plan_revision_id, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
                    """,
                    (
                        plan["identity"]["plan_revision_id"],
                        logical_key,
                        stage,
                        config_sha,
                        matrix_sha,
                        parameter_set_version,
                        existing["plan_revision_id"] if existing is not None else None,
                        canonical_json(plan),
                        now,
                    ),
                )

    def list_active_plans(self, project_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT pr.* FROM plan_revisions pr
                JOIN logical_plans lp ON lp.logical_plan_key = pr.logical_plan_key
                WHERE lp.project_id = ? AND pr.revision_status = 'ACTIVE'
                ORDER BY lp.channel, lp.note_id, lp.asset_identity
                """,
                (project_id,),
            ).fetchall()
        output: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            output.append(item)
        return output

    def enqueue_job(
        self,
        *,
        project_id: str,
        kind: str,
        payload: Any,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        input_sha = sha256_json(payload)
        input_json = canonical_json(payload)
        now = iso(utc_now())
        job_id = "job_" + uuid.uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    job_id, project_id, kind, input_sha, input_json, status,
                    attempt, max_attempts, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    project_id,
                    kind,
                    input_sha,
                    input_json,
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE project_id = ? AND kind = ? AND input_sha = ?",
                (project_id, kind, input_sha),
            ).fetchone()
        assert row is not None
        return self._job_dict(row)

    def find_job(
        self, *, project_id: str, kind: str, input_sha: str
    ) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE project_id = ? AND kind = ? AND input_sha = ?",
                (project_id, kind, input_sha),
            ).fetchone()
        return self._job_dict(row) if row is not None else None

    def claim_job(
        self, *, worker_id: str, lease_seconds: int = 60
    ) -> Optional[Dict[str, Any]]:
        now_dt = utc_now()
        now = iso(now_dt)
        lease_expires = iso(now_dt + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE (
                    (status IN ('PENDING', 'RETRY') AND next_run_at <= ?)
                    OR (status = 'RUNNING' AND lease_expires_at <= ?)
                )
                  AND attempt < max_attempts
                ORDER BY created_at, job_id
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'RUNNING', attempt = attempt + 1, lease_owner = ?,
                    lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (worker_id, lease_expires, now, now, row["job_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
        assert claimed is not None
        return self._job_dict(claimed)

    def heartbeat_job(
        self, job_id: str, *, worker_id: str, lease_seconds: int = 60
    ) -> None:
        now_dt = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'RUNNING' AND lease_owner = ?
                """,
                (
                    iso(now_dt),
                    iso(now_dt + timedelta(seconds=lease_seconds)),
                    iso(now_dt),
                    job_id,
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise SeedingError(
                    "JOB_LEASE_LOST", "job lease is not owned", status_code=409
                )

    def complete_job(self, job_id: str, *, worker_id: str) -> Dict[str, Any]:
        return self._finish_job(
            job_id, worker_id=worker_id, status="SUCCEEDED", error=None
        )

    def fail_job(self, job_id: str, *, worker_id: str, error: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt, max_attempts FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise SeedingError("JOB_NOT_FOUND", "job not found", status_code=404)
        status = "FAILED" if row["attempt"] >= row["max_attempts"] else "RETRY"
        return self._finish_job(job_id, worker_id=worker_id, status=status, error=error)

    def _finish_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        status: str,
        error: Optional[str],
    ) -> Dict[str, Any]:
        now_dt = utc_now()
        delay = timedelta(seconds=30)
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, last_error = ?, lease_owner = NULL,
                    lease_expires_at = NULL, heartbeat_at = ?, next_run_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'RUNNING' AND lease_owner = ?
                """,
                (
                    status,
                    error,
                    iso(now_dt),
                    iso(now_dt + delay),
                    iso(now_dt),
                    job_id,
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise SeedingError(
                    "JOB_LEASE_LOST", "job lease is not owned", status_code=409
                )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise SeedingError("JOB_NOT_FOUND", "job not found", status_code=404)
        return self._job_dict(row)

    def list_jobs(self, project_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [self._job_dict(row) for row in rows]

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        now = iso(utc_now())
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'CANCELLED', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND status IN ('PENDING', 'RETRY')
                """,
                (now, job_id),
            )
            if cursor.rowcount != 1:
                raise SeedingError(
                    "JOB_NOT_CANCELLABLE",
                    "only pending or retry jobs can be cancelled",
                    status_code=409,
                )
        return self.get_job(job_id)

    def resume_job(self, job_id: str) -> Dict[str, Any]:
        now = iso(utc_now())
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'PENDING', attempt = 0, last_error = NULL,
                    next_run_at = ?, updated_at = ?
                WHERE job_id = ? AND status IN ('FAILED', 'CANCELLED')
                """,
                (now, now, job_id),
            )
            if cursor.rowcount != 1:
                raise SeedingError(
                    "JOB_NOT_RESUMABLE",
                    "only failed or cancelled jobs can be resumed",
                    status_code=409,
                )
        return self.get_job(job_id)

    def _job_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["input"] = json.loads(result.pop("input_json"))
        return result

    def _append_outbox(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        event_type: str,
        payload: Any,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO outbox(
                event_id, project_id, event_type, payload_json, payload_sha, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
            """,
            (
                "event_" + uuid.uuid4().hex,
                project_id,
                event_type,
                canonical_json(payload),
                sha256_json(payload),
                now,
            ),
        )
