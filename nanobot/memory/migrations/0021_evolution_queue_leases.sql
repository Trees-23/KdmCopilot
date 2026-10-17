-- Q1 adds execution leases without deleting any candidate or source evidence.
-- The caller validates all old states before this transaction executes.
CREATE TABLE evolution_candidate_staging_v21 (
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
    'queued','manual_review','evaluating','failed',
    'proposal_eligible','adopted','closed','archived'
  )),
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_expires_at TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
  last_error TEXT,
  CHECK((status='evaluating' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status<>'evaluating' AND lease_owner IS NULL AND lease_expires_at IS NULL)),
  UNIQUE(workspace,candidate_hash)
);
INSERT INTO evolution_candidate_staging_v21 (
  candidate_id,workspace,task_id,source_kind,skill_id,skill_name,baseline_revision_id,candidate_revision_id,baseline_hash,candidate_hash,candidate_content,source_trace_ids_json,source_case_ids_json,risk_summary_json,diff_summary,rollback_revision_id,generator_version,evidence_schema_version,status,reason,created_at,updated_at,lease_owner,lease_expires_at,attempt_count,last_error
)
SELECT candidate_id,workspace,task_id,source_kind,skill_id,skill_name,baseline_revision_id,candidate_revision_id,baseline_hash,candidate_hash,candidate_content,source_trace_ids_json,source_case_ids_json,risk_summary_json,diff_summary,rollback_revision_id,generator_version,evidence_schema_version,
  CASE status
    WHEN 'candidate_staged' THEN 'queued'
    WHEN 'manual_review_required' THEN 'manual_review'
    WHEN 'rejected_by_quality_gate' THEN 'closed'
    WHEN 'insufficient_evidence' THEN 'closed'
    WHEN 'evaluating' THEN 'queued'
    ELSE status END,
  reason,created_at,updated_at,NULL,NULL,0,CASE WHEN status='evaluating' THEN 'lease_expired' ELSE NULL END
FROM evolution_candidate_staging;

-- Verify IDs, row counts and every non-state column before replacing the old table.
CREATE TEMP TABLE evolution_queue_v21_validation (valid INTEGER CHECK(valid=1));
INSERT INTO evolution_queue_v21_validation
SELECT (SELECT count(*) FROM evolution_candidate_staging) =
       (SELECT count(*) FROM evolution_candidate_staging_v21)
   AND NOT EXISTS (
  SELECT candidate_id,workspace,task_id,source_kind,skill_id,skill_name,baseline_revision_id,candidate_revision_id,baseline_hash,candidate_hash,candidate_content,source_trace_ids_json,source_case_ids_json,risk_summary_json,diff_summary,rollback_revision_id,generator_version,evidence_schema_version,reason,created_at,updated_at FROM evolution_candidate_staging
  EXCEPT SELECT candidate_id,workspace,task_id,source_kind,skill_id,skill_name,baseline_revision_id,candidate_revision_id,baseline_hash,candidate_hash,candidate_content,source_trace_ids_json,source_case_ids_json,risk_summary_json,diff_summary,rollback_revision_id,generator_version,evidence_schema_version,reason,created_at,updated_at FROM evolution_candidate_staging_v21
);
DROP TABLE evolution_candidate_staging;
ALTER TABLE evolution_candidate_staging_v21 RENAME TO evolution_candidate_staging;
CREATE INDEX evolution_candidate_staging_queue
  ON evolution_candidate_staging(workspace,status,created_at);
CREATE INDEX evolution_candidate_staging_skill
  ON evolution_candidate_staging(workspace,skill_id,skill_name,status);
CREATE INDEX evolution_candidate_staging_lease
  ON evolution_candidate_staging(workspace,status,lease_expires_at);
INSERT INTO evolution_queue_v21_validation
SELECT NOT EXISTS (SELECT 1 FROM pragma_foreign_key_check);
DROP TABLE evolution_queue_v21_validation;
