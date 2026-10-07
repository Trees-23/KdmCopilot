-- M19: retain explicit adoption state for isolated candidates.
-- Rebuild the two append-only staging tables so old rows remain auditable.

CREATE TABLE evolution_candidate_staging_v15 (
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
    'insufficient_evidence','proposal_eligible','adopted','closed'
  )),
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace,candidate_hash)
);
INSERT INTO evolution_candidate_staging_v15
SELECT candidate_id,workspace,task_id,source_kind,skill_id,skill_name,baseline_revision_id,
       candidate_revision_id,baseline_hash,candidate_hash,candidate_content,source_trace_ids_json,
       source_case_ids_json,risk_summary_json,diff_summary,rollback_revision_id,generator_version,
       evidence_schema_version,status,reason,created_at,updated_at
  FROM evolution_candidate_staging;
DROP TABLE evolution_candidate_staging;
ALTER TABLE evolution_candidate_staging_v15 RENAME TO evolution_candidate_staging;
CREATE INDEX IF NOT EXISTS evolution_candidate_staging_queue
  ON evolution_candidate_staging(workspace,status,created_at);
CREATE INDEX IF NOT EXISTS evolution_candidate_staging_skill
  ON evolution_candidate_staging(workspace,skill_id,skill_name,status);

CREATE TABLE failure_issue_candidates_v15 (
  candidate_id TEXT PRIMARY KEY,
  issue_id TEXT NOT NULL REFERENCES failure_issues(issue_id) ON DELETE CASCADE,
  workspace TEXT NOT NULL,
  skill_id TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  baseline_revision_id TEXT NOT NULL,
  candidate_revision_id TEXT NOT NULL,
  candidate_hash TEXT NOT NULL,
  candidate_content TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','evaluating','passed','adopted','failed','rejected')),
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace,issue_id)
);
INSERT INTO failure_issue_candidates_v15
SELECT candidate_id,issue_id,workspace,skill_id,skill_name,baseline_revision_id,
       candidate_revision_id,candidate_hash,candidate_content,status,reason,created_at,updated_at
  FROM failure_issue_candidates;
DROP TABLE failure_issue_candidates;
ALTER TABLE failure_issue_candidates_v15 RENAME TO failure_issue_candidates;
CREATE INDEX IF NOT EXISTS failure_issue_candidates_queue
  ON failure_issue_candidates(workspace,status,created_at);
