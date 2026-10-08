-- v17 records the raw-file hash compatibility repair for isolated candidates.
-- The byte-level backfill is performed transactionally by the migration runner.
CREATE INDEX IF NOT EXISTS failure_issue_candidates_revision_hash
  ON failure_issue_candidates(candidate_revision_id, candidate_hash);
