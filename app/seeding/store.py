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
    validate_delivery_transition,
    validate_execution_transition,
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
        migration_dir = Path(__file__).with_name("migrations")
        with self.connect() as connection:
            # The first migration creates schema_migrations itself.
            for migration in sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql")):
                version = int(migration.name.split("_", 1)[0])
                if version > 1:
                    applied = connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                    ).fetchone()
                    if applied is not None:
                        continue
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, iso(utc_now())),
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
                connection.execute(
                    """
                    INSERT INTO project_members(
                        project_id, principal_id, access_level, added_by, created_at
                    ) VALUES (?, ?, 'ADMIN', ?, ?)
                    """,
                    (
                        config.project_id,
                        owner_principal_id,
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

    def upsert_project_member(
        self,
        *,
        project_id: str,
        principal_id: str,
        access_level: str,
        added_by: str,
    ) -> Dict[str, Any]:
        now = iso(utc_now())
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO project_members(
                    project_id, principal_id, access_level, added_by, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, principal_id) DO UPDATE SET
                    access_level = excluded.access_level,
                    added_by = excluded.added_by,
                    created_at = excluded.created_at
                """,
                (project_id, principal_id, access_level, added_by, now),
            )
            self._append_outbox(
                connection,
                project_id=project_id,
                event_type="PROJECT_MEMBER_UPSERTED",
                payload={
                    "principal_id": principal_id,
                    "access_level": access_level,
                    "added_by": added_by,
                },
                now=now,
            )
        member = self.get_project_member(project_id, principal_id)
        assert member is not None
        return member

    def get_project_member(
        self, project_id: str, principal_id: str
    ) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_members WHERE project_id = ? AND principal_id = ?",
                (project_id, principal_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_project_members(self, project_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM project_members WHERE project_id = ? ORDER BY principal_id",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def transition_execution(
        self,
        project_id: str,
        target: ExecutionState,
        *,
        actor_id: str,
        reason_code: str,
    ) -> Dict[str, Any]:
        return self._transition_secondary_state(
            project_id=project_id,
            column="execution_state",
            surface="EXECUTION",
            target=target,
            actor_id=actor_id,
            reason_code=reason_code,
        )

    def transition_delivery(
        self,
        project_id: str,
        target: DeliveryState,
        *,
        actor_id: str,
        reason_code: str,
    ) -> Dict[str, Any]:
        return self._transition_secondary_state(
            project_id=project_id,
            column="delivery_state",
            surface="DELIVERY",
            target=target,
            actor_id=actor_id,
            reason_code=reason_code,
        )

    def _transition_secondary_state(
        self,
        *,
        project_id: str,
        column: str,
        surface: str,
        target: ExecutionState | DeliveryState,
        actor_id: str,
        reason_code: str,
    ) -> Dict[str, Any]:
        if column not in {"execution_state", "delivery_state"}:
            raise ValueError("invalid secondary state column")
        now = iso(utc_now())
        with self.transaction() as connection:
            row = connection.execute(
                f"SELECT {column} FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise SeedingError(
                    "PROJECT_NOT_FOUND", "project not found", status_code=404
                )
            if column == "execution_state":
                current = ExecutionState(row[column])
                assert isinstance(target, ExecutionState)
                validate_execution_transition(current, target)
            else:
                current = DeliveryState(row[column])
                assert isinstance(target, DeliveryState)
                validate_delivery_transition(current, target)
            connection.execute(
                f"UPDATE projects SET {column} = ?, updated_at = ? WHERE project_id = ?",
                (target.value, now, project_id),
            )
            transition_id = "tr_" + uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO state_transitions(
                    transition_id, project_id, state_surface, state_from, state_to,
                    actor_id, reason_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    project_id,
                    surface,
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
                event_type=f"{surface}_STATE_CHANGED",
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

    def issue_grant(
        self,
        *,
        grant_id: str,
        project_id: str,
        grant_type: str,
        payload: Any,
        expires_at: datetime,
    ) -> Dict[str, Any]:
        now = iso(utc_now())
        payload_json = canonical_json(payload)
        payload_sha = sha256_json(payload)
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO grants(
                        grant_id, project_id, grant_type, status, payload_json,
                        payload_sha, issued_at, expires_at, nonce
                    ) VALUES (?, ?, ?, 'ISSUED', ?, ?, ?, ?, ?)
                    """,
                    (
                        grant_id,
                        project_id,
                        grant_type,
                        payload_json,
                        payload_sha,
                        now,
                        iso(expires_at),
                        payload.get("nonce") if isinstance(payload, dict) else None,
                    ),
                )
                self._append_outbox(
                    connection,
                    project_id=project_id,
                    event_type=f"{grant_type}_GRANT_ISSUED",
                    payload={"grant_id": grant_id, "payload_sha": payload_sha},
                    now=now,
                )
            except sqlite3.IntegrityError as exc:
                raise SeedingError(
                    "GRANT_ALREADY_EXISTS",
                    "grant id already exists",
                    status_code=409,
                    details={"grant_id": grant_id},
                ) from exc
        return self.get_grant(grant_id)

    def get_grant(self, grant_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        if row is None:
            raise SeedingError("GRANT_NOT_FOUND", "grant not found", status_code=404)
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def list_grants(self, project_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM grants WHERE project_id = ? ORDER BY issued_at DESC",
                (project_id,),
            ).fetchall()
        output: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            output.append(item)
        return output

    def revoke_grant(
        self, grant_id: str, *, actor_id: str, reason: str
    ) -> Dict[str, Any]:
        now = iso(utc_now())
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT project_id FROM grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if row is None:
                raise SeedingError(
                    "GRANT_NOT_FOUND", "grant not found", status_code=404
                )
            cursor = connection.execute(
                """
                UPDATE grants
                SET status = 'REVOKED', revoked_at = ?, revoked_reason = ?
                WHERE grant_id = ? AND status = 'ISSUED'
                """,
                (now, reason[:1000], grant_id),
            )
            if cursor.rowcount != 1:
                raise SeedingError(
                    "GRANT_NOT_REVOCABLE",
                    "only an issued grant can be revoked",
                    status_code=409,
                )
            self._append_outbox(
                connection,
                project_id=row["project_id"],
                event_type="GRANT_REVOKED",
                payload={"grant_id": grant_id, "actor_id": actor_id, "reason": reason},
                now=now,
            )
        return self.get_grant(grant_id)

    def consume_grant_and_enqueue_job(
        self,
        *,
        grant_id: str,
        expected_grant_type: str,
        project_id: str,
        kind: str,
        payload: Any,
        max_attempts: int,
    ) -> Dict[str, Any]:
        """Atomically consume a grant and create an idempotent durable job."""

        input_sha = sha256_json(payload)
        input_json = canonical_json(payload)
        now_dt = utc_now()
        now = iso(now_dt)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE project_id = ? AND kind = ? AND input_sha = ?",
                (project_id, kind, input_sha),
            ).fetchone()
            if existing is not None:
                return self._job_dict(existing)
            grant = connection.execute(
                "SELECT * FROM grants WHERE grant_id = ? AND project_id = ?",
                (grant_id, project_id),
            ).fetchone()
            if grant is None:
                raise SeedingError(
                    "GRANT_NOT_FOUND", "grant not found", status_code=404
                )
            if grant["grant_type"] != expected_grant_type:
                raise SeedingError(
                    "GRANT_TYPE_MISMATCH",
                    "grant type does not authorize this operation",
                    status_code=409,
                )
            if grant["status"] != "ISSUED":
                raise SeedingError(
                    "GRANT_NOT_ACTIVE", "grant is not active", status_code=409
                )
            if datetime.fromisoformat(grant["expires_at"]) <= now_dt:
                connection.execute(
                    "UPDATE grants SET status = 'EXPIRED' WHERE grant_id = ?",
                    (grant_id,),
                )
                raise SeedingError(
                    "GRANT_EXPIRED", "grant has expired", status_code=409
                )
            cursor = connection.execute(
                """
                UPDATE grants SET status = 'CONSUMED', consumed_at = ?
                WHERE grant_id = ? AND status = 'ISSUED'
                """,
                (now, grant_id),
            )
            if cursor.rowcount != 1:
                raise SeedingError(
                    "GRANT_CONSUME_CONFLICT",
                    "grant was consumed concurrently",
                    status_code=409,
                )
            job_id = "job_" + uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO jobs(
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
            self._append_outbox(
                connection,
                project_id=project_id,
                event_type=f"{expected_grant_type}_GRANT_CONSUMED",
                payload={"grant_id": grant_id, "job_id": job_id},
                now=now,
            )
            created = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert created is not None
        return self._job_dict(created)

    def record_execution_result(
        self,
        *,
        project_id: str,
        grant_id: str,
        result: Dict[str, Any],
    ) -> None:
        now = iso(utc_now())
        with self.transaction() as connection:
            for index, receipt in enumerate(result.get("plan_receipts", []), start=1):
                attempt_id = "exec_" + uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO execution_attempts(
                        execution_attempt_id, project_id, plan_revision_id, grant_id,
                        request_sha, response_sha, status, started_at, finished_at,
                        readback_receipt_sha, relock_receipt_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        project_id,
                        receipt["plan_revision_id"],
                        grant_id,
                        receipt["platform_request_sha256"],
                        receipt.get("actual_sha256"),
                        result["status"],
                        now,
                        now,
                        sha256_json(receipt),
                        result.get("relock_receipt_id"),
                    ),
                )
                object_ids = receipt.get("object_ids") or {}
                typed_ids = []
                if object_ids.get("campaign_id"):
                    typed_ids.append(("CAMPAIGN", object_ids["campaign_id"]))
                if object_ids.get("unit_id"):
                    typed_ids.append(("UNIT", object_ids["unit_id"]))
                typed_ids.extend(
                    ("CREATIVITY", value)
                    for value in object_ids.get("creativity_ids", [])
                )
                for object_type, value in typed_ids:
                    platform_object_id = f"{project_id}:{object_type.lower()}:{value}"
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO platform_objects(
                            platform_object_id, execution_attempt_id, object_type,
                            expected_json, actual_json, pause_state, lock_state,
                            receipt_sha, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            platform_object_id,
                            attempt_id,
                            object_type,
                            canonical_json(
                                {"expected_sha256": receipt["expected_sha256"]}
                            ),
                            canonical_json(
                                {"actual_sha256": receipt.get("actual_sha256")}
                            ),
                            "VERIFIED_PAUSED"
                            if receipt.get("pause_verified")
                            else "UNVERIFIED",
                            "LOCKED" if result.get("lock_verified") else "UNVERIFIED",
                            sha256_json(receipt),
                            now,
                        ),
                    )
            self._append_outbox(
                connection,
                project_id=project_id,
                event_type="TEST_EXECUTION_RECORDED",
                payload={
                    "grant_id": grant_id,
                    "status": result["status"],
                    "receipt_sha": sha256_json(result),
                },
                now=now,
            )

    def list_execution_attempts(self, project_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_attempts WHERE project_id = ? ORDER BY started_at",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_execution_attempts(self, project_id: Optional[str] = None) -> int:
        with self.connect() as connection:
            if project_id is None:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM execution_attempts"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM execution_attempts WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
        return int(row["count"])

    def operational_metrics(self) -> Dict[str, Any]:
        with self.connect() as connection:
            project_rows = connection.execute(
                "SELECT project_state, execution_state, delivery_state, COUNT(*) AS count FROM projects GROUP BY project_state, execution_state, delivery_state"
            ).fetchall()
            job_rows = connection.execute(
                "SELECT kind, status, COUNT(*) AS count FROM jobs GROUP BY kind, status"
            ).fetchall()
            grant_rows = connection.execute(
                "SELECT grant_type, status, COUNT(*) AS count FROM grants GROUP BY grant_type, status"
            ).fetchall()
            pending_outbox = connection.execute(
                "SELECT COUNT(*) AS count FROM outbox WHERE status = 'PENDING'"
            ).fetchone()["count"]
            execution_attempts = connection.execute(
                "SELECT COUNT(*) AS count FROM execution_attempts"
            ).fetchone()["count"]
        return {
            "projects": [dict(row) for row in project_rows],
            "jobs": [dict(row) for row in job_rows],
            "grants": [dict(row) for row in grant_rows],
            "pending_outbox": pending_outbox,
            "execution_attempts": execution_attempts,
        }

    def audit_trail(self, project_id: str, *, limit: int = 200) -> Dict[str, Any]:
        if not 1 <= limit <= 500:
            raise ValueError("audit limit must be between 1 and 500")
        with self.connect() as connection:
            transitions = connection.execute(
                "SELECT * FROM state_transitions WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
            events = connection.execute(
                "SELECT * FROM outbox WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        event_items = []
        for row in events:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            event_items.append(item)
        return {
            "state_transitions": [dict(row) for row in transitions],
            "outbox_events": event_items,
        }

    def emit_event(self, *, project_id: str, event_type: str, payload: Any) -> None:
        now = iso(utc_now())
        with self.transaction() as connection:
            self._append_outbox(
                connection,
                project_id=project_id,
                event_type=event_type,
                payload=payload,
                now=now,
            )

    def claim_outbox(
        self, *, worker_id: str, limit: int = 50, lease_seconds: int = 60
    ) -> List[Dict[str, Any]]:
        now_dt = utc_now()
        now = iso(now_dt)
        lease_expires = iso(now_dt + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox
                WHERE (
                    (status IN ('PENDING', 'RETRY') AND next_attempt_at <= ?)
                    OR (status = 'PROCESSING' AND lease_expires_at <= ?)
                )
                ORDER BY created_at, event_id
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE outbox SET status = 'PROCESSING', attempt = attempt + 1,
                        lease_owner = ?, lease_expires_at = ?
                    WHERE event_id = ?
                    """,
                    (worker_id, lease_expires, row["event_id"]),
                )
            claimed = [
                connection.execute(
                    "SELECT * FROM outbox WHERE event_id = ?", (row["event_id"],)
                ).fetchone()
                for row in rows
            ]
        output = []
        for row in claimed:
            assert row is not None
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            output.append(item)
        return output

    def complete_outbox(self, event_id: str, *, worker_id: str) -> None:
        now = iso(utc_now())
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox SET status = 'PUBLISHED', published_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, last_error = NULL
                WHERE event_id = ? AND status = 'PROCESSING' AND lease_owner = ?
                """,
                (now, event_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise SeedingError(
                    "OUTBOX_LEASE_LOST", "outbox lease is not owned", status_code=409
                )

    def fail_outbox(self, event_id: str, *, worker_id: str, error: str) -> None:
        next_attempt = iso(utc_now() + timedelta(seconds=30))
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox SET status = 'RETRY', next_attempt_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, last_error = ?
                WHERE event_id = ? AND status = 'PROCESSING' AND lease_owner = ?
                """,
                (next_attempt, error[:1000], event_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise SeedingError(
                    "OUTBOX_LEASE_LOST", "outbox lease is not owned", status_code=409
                )

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
            # Never replay an indeterminate external write after a worker lease
            # expires. A human/operator must call the reconcile flow instead.
            indeterminate = connection.execute(
                """
                SELECT job_id, project_id FROM jobs
                WHERE status = 'RUNNING' AND lease_expires_at <= ?
                  AND kind IN (
                      'EXECUTE_TEST', 'PRODUCTION_CREATE', 'RECONCILE',
                      'RELEASE', 'RELEASE_EVALUATE', 'RELEASE_RECOVERY'
                  )
                """,
                (now,),
            ).fetchall()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'RECONCILE_REQUIRED', lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = 'worker lease expired during external write; reconcile required',
                    updated_at = ?
                WHERE status = 'RUNNING' AND lease_expires_at <= ?
                  AND kind IN (
                      'EXECUTE_TEST', 'PRODUCTION_CREATE', 'RECONCILE',
                      'RELEASE', 'RELEASE_EVALUATE', 'RELEASE_RECOVERY'
                  )
                """,
                (now, now),
            )
            for expired in indeterminate:
                state = connection.execute(
                    "SELECT execution_state FROM projects WHERE project_id = ?",
                    (expired["project_id"],),
                ).fetchone()
                if (
                    state is not None
                    and state["execution_state"] == "WRITE_IN_PROGRESS"
                ):
                    connection.execute(
                        "UPDATE projects SET execution_state = 'RECONCILE_REQUIRED', updated_at = ? WHERE project_id = ?",
                        (now, expired["project_id"]),
                    )
                    transition_id = "tr_" + uuid.uuid4().hex
                    connection.execute(
                        """
                        INSERT INTO state_transitions(
                            transition_id, project_id, state_surface, state_from,
                            state_to, actor_id, reason_code, created_at
                        ) VALUES (?, ?, 'EXECUTION', 'WRITE_IN_PROGRESS',
                                  'RECONCILE_REQUIRED', 'durable-worker',
                                  'EXTERNAL_WRITE_LEASE_EXPIRED', ?)
                        """,
                        (transition_id, expired["project_id"], now),
                    )
                    self._append_outbox(
                        connection,
                        project_id=expired["project_id"],
                        event_type="EXECUTION_STATE_CHANGED",
                        payload={
                            "transition_id": transition_id,
                            "from": "WRITE_IN_PROGRESS",
                            "to": "RECONCILE_REQUIRED",
                            "reason_code": "EXTERNAL_WRITE_LEASE_EXPIRED",
                            "job_id": expired["job_id"],
                        },
                        now=now,
                    )
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

    def heartbeat_worker_jobs(self, *, worker_id: str, lease_seconds: int = 60) -> int:
        """Extend every running lease owned by one in-process worker."""

        now_dt = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE status = 'RUNNING' AND lease_owner = ?
                """,
                (
                    iso(now_dt),
                    iso(now_dt + timedelta(seconds=lease_seconds)),
                    iso(now_dt),
                    worker_id,
                ),
            )
        return int(cursor.rowcount)

    def complete_job(
        self, job_id: str, *, worker_id: str, result: Any = None
    ) -> Dict[str, Any]:
        return self._finish_job(
            job_id, worker_id=worker_id, status="SUCCEEDED", error=None, result=result
        )

    def fail_terminal_job(
        self, job_id: str, *, worker_id: str, result: Any
    ) -> Dict[str, Any]:
        return self._finish_job(
            job_id,
            worker_id=worker_id,
            status="FAILED",
            error="external operation completed with a fail-closed outcome",
            result=result,
        )

    def require_reconcile_job(
        self, job_id: str, *, worker_id: str, result: Any
    ) -> Dict[str, Any]:
        return self._finish_job(
            job_id,
            worker_id=worker_id,
            status="RECONCILE_REQUIRED",
            error="external result is indeterminate; explicit reconciliation required",
            result=result,
        )

    def fail_job(self, job_id: str, *, worker_id: str, error: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt, max_attempts FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise SeedingError("JOB_NOT_FOUND", "job not found", status_code=404)
        status = "FAILED" if row["attempt"] >= row["max_attempts"] else "RETRY"
        return self._finish_job(
            job_id, worker_id=worker_id, status=status, error=error, result=None
        )

    def _finish_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        status: str,
        error: Optional[str],
        result: Any,
    ) -> Dict[str, Any]:
        now_dt = utc_now()
        delay = timedelta(seconds=30)
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, last_error = ?, result_json = ?, lease_owner = NULL,
                    lease_expires_at = NULL, heartbeat_at = ?, next_run_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'RUNNING' AND lease_owner = ?
                """,
                (
                    status,
                    error,
                    canonical_json(result) if result is not None else None,
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
        raw_result = result.pop("result_json", None)
        result["result"] = json.loads(raw_result) if raw_result else None
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
                event_id, project_id, event_type, payload_json, payload_sha, status,
                created_at, next_attempt_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)
            """,
            (
                "event_" + uuid.uuid4().hex,
                project_id,
                event_type,
                canonical_json(payload),
                sha256_json(payload),
                now,
                now,
            ),
        )
