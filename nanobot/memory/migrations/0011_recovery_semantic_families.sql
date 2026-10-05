-- M17: structured recovery classification.  This migration is append-only;
-- old recovery rows remain auditable and are classified with a safe fallback.
ALTER TABLE recovery_episodes ADD COLUMN operation_family TEXT NOT NULL DEFAULT 'unknown_operation';
ALTER TABLE recovery_episodes ADD COLUMN failure_family TEXT NOT NULL DEFAULT 'unknown_failure';
ALTER TABLE recovery_episodes ADD COLUMN task_family TEXT NOT NULL DEFAULT 'unknown_task';
ALTER TABLE recovery_episodes ADD COLUMN correction_family TEXT NOT NULL DEFAULT 'unknown_correction';
ALTER TABLE recovery_episodes ADD COLUMN classification_version TEXT NOT NULL DEFAULT 'm17-v1';
ALTER TABLE recovery_episodes ADD COLUMN failure_error_code TEXT;
ALTER TABLE recovery_episodes ADD COLUMN failure_error_type TEXT;
ALTER TABLE recovery_episodes ADD COLUMN failure_error_source TEXT;
ALTER TABLE recovery_episodes ADD COLUMN failure_retryability TEXT;
CREATE INDEX IF NOT EXISTS recovery_episodes_semantic_family
  ON recovery_episodes(workspace,operation_family,failure_family,task_family,correction_family,recovered_at);
