-- M19: isolated candidate staging.  Candidates never update the active Skill
-- revision. Proposal/Adoption/Overlay state remains owned by existing tables.
CREATE TABLE IF NOT EXISTS evolution_candidate_staging (
  candidate_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  task_id TEXT NOT NULL REFERENCES evolution_task_evidence(task_id) ON DELETE RESTRICT,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('skill_candidate','skill_revision_candidate')),
  skill_id TEXT,
  skill_name TEXT NOT NULL,
  baseline_revision_id TEXT,
  candidate_revision_id TEXT NOT NULL,
  baseline_hash TEXT,
  candidate_hash TEXT NOT NULL,
  candidate_content TEXT NOT NULL,
  source_trace_ids_json TEXT NOT NULL DEFAULT '[]',
  source_case_ids_json TEXT NOT NULL DEFAULT '[]',
  risk_summary_json TEXT NOT NULL DEFAULT '{}',
  diff_summary TEXT NOT NULL DEFAULT '',
  rollback_revision_id TEXT,
  generator_version TEXT NOT NULL DEFAULT 'm19-v1',
  evidence_schema_version TEXT NOT NULL DEFAULT 'm18-v1',
  status TEXT NOT NULL CHECK(status IN (
    'candidate_staged','manual_review_required','evaluating','rejected_by_quality_gate',
    'insufficient_evidence','proposal_eligible','closed'
  )),
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace,candidate_hash)
);
CREATE INDEX IF NOT EXISTS evolution_candidate_staging_queue
  ON evolution_candidate_staging(workspace,status,created_at);
CREATE INDEX IF NOT EXISTS evolution_candidate_staging_skill
  ON evolution_candidate_staging(workspace,skill_id,skill_name,status);
