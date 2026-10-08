-- v18 binds existing M15 audit rows to the corrected raw SKILL.md hash.
-- The transactionally consistent data repair is performed by the migration runner.
CREATE INDEX IF NOT EXISTS ab_evaluations_workspace_candidate_hash
  ON ab_evaluations(workspace, candidate_hash, created_at);
