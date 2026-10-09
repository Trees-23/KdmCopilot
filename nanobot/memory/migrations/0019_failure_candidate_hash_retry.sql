-- v19 permits a retry only for v17-corrected candidates that had already
-- passed M15 and were blocked solely by the former hash representation.
CREATE INDEX IF NOT EXISTS skill_proposals_candidate_status
  ON skill_proposals(candidate_revision_id, status);
