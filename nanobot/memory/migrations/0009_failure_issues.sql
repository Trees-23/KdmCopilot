CREATE TABLE IF NOT EXISTS failure_issues (
  issue_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  recovery_key TEXT NOT NULL,
  episode_ids_json TEXT NOT NULL,
  title TEXT NOT NULL,
  task_goal TEXT,
  failure_class TEXT NOT NULL,
  correction_goal TEXT,
  summary TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending_review','recorded_case','memory_recorded','candidate_requested','rejected','insufficient_evidence','expired')),
  recommendation TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reviewed_at TEXT,
  reviewed_by TEXT,
  review_reason TEXT,
  UNIQUE(workspace,recovery_key,episode_ids_json)
);
CREATE INDEX IF NOT EXISTS failure_issues_status
  ON failure_issues(workspace,status,created_at DESC);

CREATE TABLE IF NOT EXISTS failure_issue_actions (
  action_id TEXT PRIMARY KEY,
  issue_id TEXT NOT NULL REFERENCES failure_issues(issue_id) ON DELETE CASCADE,
  workspace TEXT NOT NULL,
  actor_openid TEXT NOT NULL,
  group_openid TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('note','record_case','record_memory','request_candidate','reject')),
  note TEXT NOT NULL DEFAULT '',
  result_status TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS failure_issue_actions_issue
  ON failure_issue_actions(issue_id,created_at DESC);

CREATE TABLE IF NOT EXISTS failure_issue_deliveries (
  delivery_id TEXT PRIMARY KEY,
  issue_id TEXT NOT NULL REFERENCES failure_issues(issue_id) ON DELETE CASCADE,
  workspace TEXT NOT NULL,
  group_openid TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','leased','sent','retry','dead_letter')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  lease_until TEXT,
  last_error TEXT,
  audit_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(issue_id,group_openid)
);
CREATE INDEX IF NOT EXISTS failure_issue_deliveries_queue
  ON failure_issue_deliveries(status,next_attempt_at,created_at);
