CREATE TABLE IF NOT EXISTS recovery_episodes (
  episode_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  session_key TEXT NOT NULL,
  failure_trace_id TEXT NOT NULL UNIQUE,
  recovery_trace_id TEXT UNIQUE,
  failure_goal TEXT,
  failure_intent TEXT,
  failure_class TEXT NOT NULL,
  failure_tools_json TEXT NOT NULL DEFAULT '[]',
  correction_goal TEXT,
  correction_intent TEXT,
  recovery_tools_json TEXT NOT NULL DEFAULT '[]',
  redaction_status TEXT NOT NULL CHECK(redaction_status IN ('safe','rejected')),
  status TEXT NOT NULL CHECK(status IN ('failed','recovered','insufficient_recovery_evidence')),
  rejection_reason TEXT,
  created_at TEXT NOT NULL,
  recovered_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recovery_episodes_pending
  ON recovery_episodes(workspace,session_key,status,created_at DESC);
CREATE INDEX IF NOT EXISTS recovery_episodes_review
  ON recovery_episodes(workspace,status,failure_class,created_at);

CREATE TABLE IF NOT EXISTS recovery_case_reviews (
  review_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  recovery_key TEXT NOT NULL,
  episode_ids_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('passed','insufficient_recovery_evidence','rejected_by_quality_gate')),
  reason_code TEXT NOT NULL,
  reason_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(workspace,recovery_key,episode_ids_json)
);
CREATE INDEX IF NOT EXISTS recovery_case_reviews_status
  ON recovery_case_reviews(workspace,status,created_at DESC);
