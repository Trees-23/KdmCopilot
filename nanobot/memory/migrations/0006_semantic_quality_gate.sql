CREATE TABLE IF NOT EXISTS semantic_task_evidence (
  evidence_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  trace_id TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL,
  session_key TEXT,
  source_type TEXT NOT NULL,
  outcome TEXT NOT NULL,
  task_goal TEXT,
  intent TEXT,
  input_scope TEXT,
  expected_outcome TEXT,
  operation_signature_json TEXT NOT NULL DEFAULT '[]',
  semantic_key TEXT,
  redaction_status TEXT NOT NULL CHECK(redaction_status IN ('safe','rejected')),
  evidence_status TEXT NOT NULL CHECK(evidence_status IN ('eligible','insufficient_semantic_evidence','rejected_by_quality_gate')),
  rejection_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS semantic_task_evidence_candidate
  ON semantic_task_evidence(workspace,evidence_status,semantic_key,created_at);

CREATE TABLE IF NOT EXISTS semantic_quality_reviews (
  review_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  semantic_key TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('passed','rejected_by_quality_gate')),
  reason_code TEXT NOT NULL,
  reason_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(workspace,semantic_key,evidence_ids_json)
);
CREATE INDEX IF NOT EXISTS semantic_quality_reviews_status
  ON semantic_quality_reviews(workspace,status,created_at);
