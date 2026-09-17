ALTER TABLE outbox ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN next_attempt_at TEXT;
ALTER TABLE outbox ADD COLUMN lease_owner TEXT;
ALTER TABLE outbox ADD COLUMN lease_expires_at TEXT;
ALTER TABLE outbox ADD COLUMN last_error TEXT;

UPDATE outbox SET next_attempt_at = created_at WHERE next_attempt_at IS NULL;
CREATE INDEX ix_outbox_delivery
ON outbox(status, next_attempt_at, lease_expires_at);
