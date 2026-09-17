ALTER TABLE grants ADD COLUMN revoked_reason TEXT;
ALTER TABLE grants ADD COLUMN nonce TEXT;
CREATE UNIQUE INDEX uq_grant_nonce ON grants(project_id, grant_type, nonce);
ALTER TABLE execution_attempts ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 1;
ALTER TABLE execution_attempts ADD COLUMN readback_receipt_sha TEXT;
ALTER TABLE execution_attempts ADD COLUMN relock_receipt_id TEXT;
ALTER TABLE jobs ADD COLUMN result_json TEXT;
