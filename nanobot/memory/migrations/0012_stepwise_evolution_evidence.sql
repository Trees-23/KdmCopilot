-- M18: redacted task-level and ordered step-level evolution evidence.
-- This migration is append-only.  It does not alter the existing semantic,
-- recovery, Proposal or active Skill contracts.
CREATE TABLE IF NOT EXISTS evolution_task_evidence (
  task_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  session_key TEXT,
  source_trace_ids_json TEXT NOT NULL DEFAULT '[]',
  goal TEXT,
  intent TEXT,
  scope TEXT,
  expected_outcome TEXT,
  actual_outcome TEXT,
  verification_status TEXT NOT NULL DEFAULT 'unverified',
  qualification TEXT NOT NULL CHECK(qualification IN (
    'candidate_eligible','partial_evidence','manual_review_required','unsafe_for_skill'
  )),
  legacy_qualification TEXT NOT NULL DEFAULT 'unknown',
  redaction_status TEXT NOT NULL CHECK(redaction_status IN ('safe','rejected')),
  schema_version TEXT NOT NULL DEFAULT 'm18-v1',
  rejection_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace,task_id)
);
CREATE INDEX IF NOT EXISTS evolution_task_evidence_candidate
  ON evolution_task_evidence(workspace,qualification,updated_at);

CREATE TABLE IF NOT EXISTS evolution_step_evidence (
  step_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES evolution_task_evidence(task_id) ON DELETE CASCADE,
  workspace TEXT NOT NULL,
  sequence_no INTEGER NOT NULL CHECK(sequence_no >= 0),
  trace_id TEXT,
  event_id TEXT,
  tool_name TEXT NOT NULL,
  operation_kind TEXT NOT NULL DEFAULT 'unknown',
  input_summary TEXT,
  output_summary TEXT,
  status TEXT NOT NULL,
  failure_family TEXT,
  side_effect_class TEXT NOT NULL DEFAULT 'none',
  risk_level TEXT NOT NULL CHECK(risk_level IN ('R0','R1','R2','R3','R4')),
  resource_key TEXT,
  verification_kind TEXT,
  candidate_use TEXT NOT NULL CHECK(candidate_use IN ('reusable','abstract_only','archive_only','unverified')),
  redaction_status TEXT NOT NULL CHECK(redaction_status IN ('safe','rejected')),
  created_at TEXT NOT NULL,
  UNIQUE(task_id,sequence_no)
);
CREATE INDEX IF NOT EXISTS evolution_step_evidence_lookup
  ON evolution_step_evidence(task_id,sequence_no);
CREATE INDEX IF NOT EXISTS evolution_step_evidence_risk
  ON evolution_step_evidence(workspace,risk_level,created_at);

CREATE TABLE IF NOT EXISTS evolution_task_links (
  link_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES evolution_task_evidence(task_id) ON DELETE CASCADE,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(task_id,source_type,source_id,relation)
);
CREATE INDEX IF NOT EXISTS evolution_task_links_source
  ON evolution_task_links(source_type,source_id);
