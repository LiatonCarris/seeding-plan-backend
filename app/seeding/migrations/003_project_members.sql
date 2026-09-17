CREATE TABLE project_members (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    principal_id TEXT NOT NULL,
    access_level TEXT NOT NULL CHECK(access_level IN ('READ', 'WRITE', 'APPROVE', 'EXECUTE', 'ADMIN')),
    added_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, principal_id)
);

CREATE INDEX ix_project_members_principal
ON project_members(principal_id, project_id);

INSERT OR IGNORE INTO project_members(
    project_id, principal_id, access_level, added_by, created_at
)
SELECT project_id, owner_principal_id, 'ADMIN', owner_principal_id, created_at
FROM projects;
