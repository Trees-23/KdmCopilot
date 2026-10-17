-- Keep resource categories and normalized failure facts at their original source.
ALTER TABLE evolution_step_evidence ADD COLUMN resource_class TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE evolution_step_evidence ADD COLUMN error_code TEXT;
ALTER TABLE evolution_step_evidence ADD COLUMN error_type TEXT;
ALTER TABLE evolution_step_evidence ADD COLUMN error_source TEXT;
ALTER TABLE evolution_step_evidence ADD COLUMN retryability TEXT CHECK(retryability IS NULL OR retryability IN ('retryable','non_retryable','unknown'));
