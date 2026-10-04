CREATE TABLE IF NOT EXISTS failure_issue_candidates (
  candidate_id TEXT PRIMARY KEY,
  issue_id TEXT NOT NULL REFERENCES failure_issues(issue_id) ON DELETE CASCADE,
  workspace TEXT NOT NULL,
  skill_id TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  baseline_revision_id TEXT NOT NULL,
  candidate_revision_id TEXT NOT NULL,
  candidate_hash TEXT NOT NULL,
  candidate_content TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','evaluating','passed','failed','rejected')),
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace,issue_id)
);
CREATE INDEX IF NOT EXISTS failure_issue_candidates_queue
  ON failure_issue_candidates(workspace,status,created_at);
