PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    owner_principal_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_sha TEXT NOT NULL,
    project_state TEXT NOT NULL,
    execution_state TEXT NOT NULL,
    delivery_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_revisions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    revision INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    config_sha TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, revision),
    UNIQUE (project_id, config_sha)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    kind TEXT NOT NULL,
    input_sha TEXT NOT NULL,
    content_sha TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, kind, input_sha)
);

CREATE TABLE IF NOT EXISTS logical_plans (
    logical_plan_key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    stage TEXT NOT NULL,
    channel TEXT NOT NULL,
    note_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    asset_identity TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_revisions (
    plan_revision_id TEXT PRIMARY KEY,
    logical_plan_key TEXT NOT NULL REFERENCES logical_plans(logical_plan_key),
    stage TEXT NOT NULL,
    config_sha TEXT NOT NULL,
    matrix_sha TEXT NOT NULL,
    parameter_set_version INTEGER NOT NULL,
    revision_status TEXT NOT NULL,
    supersedes_plan_revision_id TEXT REFERENCES plan_revisions(plan_revision_id),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_plan_revision
ON plan_revisions(logical_plan_key, stage)
WHERE revision_status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    grant_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS execution_attempts (
    execution_attempt_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(plan_revision_id),
    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
    request_sha TEXT NOT NULL,
    response_sha TEXT,
    status TEXT NOT NULL,
    platform_request_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (execution_attempt_id, request_sha)
);

CREATE TABLE IF NOT EXISTS platform_objects (
    platform_object_id TEXT PRIMARY KEY,
    execution_attempt_id TEXT NOT NULL REFERENCES execution_attempts(execution_attempt_id),
    object_type TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    actual_json TEXT,
    pause_state TEXT,
    lock_state TEXT,
    receipt_sha TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_transitions (
    transition_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    state_surface TEXT NOT NULL,
    state_from TEXT NOT NULL,
    state_to TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    kind TEXT NOT NULL,
    input_sha TEXT NOT NULL,
    input_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    next_run_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, kind, input_sha)
);

CREATE INDEX IF NOT EXISTS ix_jobs_claim
ON jobs(status, next_run_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT
);
