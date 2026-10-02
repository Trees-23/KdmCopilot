CREATE TABLE IF NOT EXISTS ab_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  candidate_hash TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('shadow','enforced')),
  model_id TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL,
  real_evidence_count INTEGER NOT NULL,
  max_model_calls INTEGER NOT NULL,
  model_calls_used INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN ('running','passed','failed','budget_exhausted','error')),
  reason_code TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS ab_evaluations_daily_budget
  ON ab_evaluations(workspace,created_at,status);
CREATE INDEX IF NOT EXISTS ab_evaluations_candidate
  ON ab_evaluations(workspace,candidate_hash,status);

CREATE TABLE IF NOT EXISTS ab_case_results (
  result_id TEXT PRIMARY KEY,
  evaluation_id TEXT NOT NULL REFERENCES ab_evaluations(evaluation_id) ON DELETE CASCADE,
  case_key TEXT NOT NULL,
  split TEXT NOT NULL CHECK(split IN ('positive','scope_negative','safety_negative')),
  attempt INTEGER NOT NULL,
  baseline_digest TEXT NOT NULL,
  candidate_digest TEXT NOT NULL,
  baseline_score REAL,
  candidate_score REAL,
  baseline_tokens INTEGER NOT NULL DEFAULT 0,
  candidate_tokens INTEGER NOT NULL DEFAULT 0,
  baseline_latency_ms REAL NOT NULL DEFAULT 0,
  candidate_latency_ms REAL NOT NULL DEFAULT 0,
  baseline_tools_json TEXT NOT NULL DEFAULT '[]',
  candidate_tools_json TEXT NOT NULL DEFAULT '[]',
  safety_clean INTEGER NOT NULL CHECK(safety_clean IN (0,1)),
  judge_status TEXT NOT NULL CHECK(judge_status IN ('passed','failed','uncertain','not_required')),
  judge_digest TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(evaluation_id,case_key,attempt)
);
CREATE INDEX IF NOT EXISTS ab_case_results_evaluation
  ON ab_case_results(evaluation_id,split,attempt);
