-- Q2 records independent rounds and atomic daily budget reservations.
CREATE TABLE ab_evaluations_v28 (
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
  status TEXT NOT NULL CHECK(status IN ('running','passed','failed','budget_exhausted','error','uncertain')),
  reason_code TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  completed_at TEXT,
  protocol_version TEXT NOT NULL DEFAULT 'legacy-v0',
  execution_deadline TEXT
);
CREATE TABLE ab_case_results_v28 (
  result_id TEXT PRIMARY KEY,
  evaluation_id TEXT NOT NULL REFERENCES ab_evaluations_v28(evaluation_id) ON DELETE CASCADE,
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
  judge_feedback TEXT NOT NULL DEFAULT '',
  sample_round INTEGER NOT NULL DEFAULT 0 CHECK(sample_round IN (0,1,2)),
  UNIQUE(evaluation_id,case_key,attempt,sample_round)
);
INSERT INTO ab_evaluations_v28 (evaluation_id,workspace,skill_name,candidate_hash,mode,model_id,reasoning_effort,real_evidence_count,max_model_calls,model_calls_used,status,reason_code,metrics_json,created_at,completed_at) SELECT evaluation_id,workspace,skill_name,candidate_hash,mode,model_id,reasoning_effort,real_evidence_count,max_model_calls,model_calls_used,status,reason_code,metrics_json,created_at,completed_at FROM ab_evaluations;
INSERT INTO ab_case_results_v28 (result_id,evaluation_id,case_key,split,attempt,baseline_digest,candidate_digest,baseline_score,candidate_score,baseline_tokens,candidate_tokens,baseline_latency_ms,candidate_latency_ms,baseline_tools_json,candidate_tools_json,safety_clean,judge_status,judge_digest,created_at,judge_feedback) SELECT result_id,evaluation_id,case_key,split,attempt,baseline_digest,candidate_digest,baseline_score,candidate_score,baseline_tokens,candidate_tokens,baseline_latency_ms,candidate_latency_ms,baseline_tools_json,candidate_tools_json,safety_clean,judge_status,judge_digest,created_at,judge_feedback FROM ab_case_results;
CREATE TEMP TABLE evaluation_v28_validation (valid INTEGER CHECK(valid=1));
INSERT INTO evaluation_v28_validation SELECT (SELECT count(*) FROM ab_evaluations)=(SELECT count(*) FROM ab_evaluations_v28) AND NOT EXISTS (SELECT evaluation_id,workspace,skill_name,candidate_hash,mode,model_id,reasoning_effort,real_evidence_count,max_model_calls,model_calls_used,status,reason_code,metrics_json,created_at,completed_at FROM ab_evaluations EXCEPT SELECT evaluation_id,workspace,skill_name,candidate_hash,mode,model_id,reasoning_effort,real_evidence_count,max_model_calls,model_calls_used,status,reason_code,metrics_json,created_at,completed_at FROM ab_evaluations_v28);
INSERT INTO evaluation_v28_validation SELECT (SELECT count(*) FROM ab_case_results)=(SELECT count(*) FROM ab_case_results_v28) AND NOT EXISTS (SELECT result_id,evaluation_id,case_key,split,attempt,baseline_digest,candidate_digest,baseline_score,candidate_score,baseline_tokens,candidate_tokens,baseline_latency_ms,candidate_latency_ms,baseline_tools_json,candidate_tools_json,safety_clean,judge_status,judge_digest,created_at,judge_feedback FROM ab_case_results EXCEPT SELECT result_id,evaluation_id,case_key,split,attempt,baseline_digest,candidate_digest,baseline_score,candidate_score,baseline_tokens,candidate_tokens,baseline_latency_ms,candidate_latency_ms,baseline_tools_json,candidate_tools_json,safety_clean,judge_status,judge_digest,created_at,judge_feedback FROM ab_case_results_v28);
DROP TABLE ab_case_results;
DROP TABLE ab_evaluations;
ALTER TABLE ab_evaluations_v28 RENAME TO ab_evaluations;
ALTER TABLE ab_case_results_v28 RENAME TO ab_case_results;
CREATE INDEX ab_evaluations_daily_budget ON ab_evaluations(workspace,created_at,status);
CREATE INDEX ab_evaluations_candidate ON ab_evaluations(workspace,candidate_hash,status);
CREATE UNIQUE INDEX ab_evaluations_consistent_result
  ON ab_evaluations(workspace,candidate_hash)
  WHERE protocol_version='two-round-v1' AND status IN ('passed','failed');
CREATE UNIQUE INDEX ab_evaluations_active_content
  ON ab_evaluations(workspace,candidate_hash)
  WHERE protocol_version='two-round-v1' AND status='running';
CREATE INDEX ab_case_results_evaluation ON ab_case_results(evaluation_id,split,sample_round,attempt);

CREATE TABLE evaluation_candidate_admissions (
  workspace TEXT NOT NULL,
  budget_day TEXT NOT NULL,
  candidate_hash TEXT NOT NULL,
  admitted_evaluation_id TEXT NOT NULL REFERENCES ab_evaluations(evaluation_id) ON DELETE CASCADE,
  settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)),
  PRIMARY KEY(workspace,budget_day,candidate_hash)
);
CREATE TABLE evaluation_model_call_reservations (
  evaluation_id TEXT NOT NULL REFERENCES ab_evaluations(evaluation_id) ON DELETE CASCADE,
  budget_day TEXT NOT NULL,
  workspace TEXT NOT NULL,
  reserved_calls INTEGER NOT NULL CHECK(reserved_calls>=0),
  actual_calls INTEGER NOT NULL DEFAULT 0 CHECK(actual_calls>=0),
  unconfirmed_calls INTEGER NOT NULL DEFAULT 0 CHECK(unconfirmed_calls>=0),
  status TEXT NOT NULL CHECK(status IN ('active','settled','abandoned')),
  PRIMARY KEY(evaluation_id,budget_day)
);
CREATE INDEX evaluation_call_budget_day ON evaluation_model_call_reservations(workspace,budget_day,status);
INSERT INTO evaluation_v28_validation SELECT NOT EXISTS (SELECT 1 FROM pragma_foreign_key_check);
DROP TABLE evaluation_v28_validation;
