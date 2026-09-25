CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER PRIMARY KEY,
  migration_id TEXT NOT NULL UNIQUE,
  app_build TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','applied','failed','rolled_back')),
  applied_at TEXT NOT NULL,
  error_message TEXT
);
CREATE INDEX IF NOT EXISTS schema_meta_status ON schema_meta(status,version DESC);

CREATE TABLE IF NOT EXISTS memory_records (
  memory_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL DEFAULT 'workspace',
  memory_type TEXT NOT NULL CHECK(memory_type IN ('fact','decision','trace_summary','case','skill_ref')),
  title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
  source_refs_json TEXT NOT NULL DEFAULT '[]', entities_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]', sensitivity TEXT NOT NULL DEFAULT 'private',
  confidence REAL NOT NULL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 1),
  authority REAL NOT NULL DEFAULT 0 CHECK(authority BETWEEN 0 AND 1),
  salience REAL NOT NULL DEFAULT 0 CHECK(salience BETWEEN 0 AND 1),
  effective_score REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'candidate',
  deletion_state TEXT NOT NULL DEFAULT 'none', source_actor TEXT NOT NULL,
  current_revision_id TEXT REFERENCES memory_revisions(revision_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
  supersedes_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_accessed_at TEXT,
  access_count INTEGER NOT NULL DEFAULT 0, archived_at TEXT, last_skill_review_at TEXT
);
CREATE INDEX IF NOT EXISTS memory_scope ON memory_records(namespace,memory_type,status,effective_score DESC);
CREATE INDEX IF NOT EXISTS memory_hashless_source ON memory_records(source_actor,updated_at);

CREATE TABLE IF NOT EXISTS memory_revisions (
  revision_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
  revision_no INTEGER NOT NULL, content TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL, previous_revision_id TEXT REFERENCES memory_revisions(revision_id),
  supersedes_revision_id TEXT REFERENCES memory_revisions(revision_id),
  author_actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(memory_id,revision_no), UNIQUE(memory_id,content_hash)
);
CREATE INDEX IF NOT EXISTS memory_revision_history ON memory_revisions(memory_id,revision_no DESC);

CREATE TABLE IF NOT EXISTS wiki_pages (
  page_id TEXT PRIMARY KEY, namespace TEXT NOT NULL DEFAULT 'workspace', slug TEXT NOT NULL,
  page_type TEXT NOT NULL CHECK(page_type IN ('fact','decision','case','skill_note','index')),
  source_path TEXT NOT NULL, content_hash TEXT NOT NULL, current_revision_id TEXT,
  status TEXT NOT NULL DEFAULT 'candidate', sensitivity TEXT NOT NULL DEFAULT 'private',
  title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
  source_refs_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  archived_at TEXT, UNIQUE(namespace,slug)
);
CREATE INDEX IF NOT EXISTS wiki_lookup ON wiki_pages(namespace,page_type,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS wiki_hash ON wiki_pages(content_hash);

CREATE TABLE IF NOT EXISTS wiki_relations (
  relation_id TEXT PRIMARY KEY, from_page_id TEXT NOT NULL REFERENCES wiki_pages(page_id),
  to_page_id TEXT NOT NULL REFERENCES wiki_pages(page_id), relation_type TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
  source_revision_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(from_page_id,to_page_id,relation_type)
);
CREATE INDEX IF NOT EXISTS wiki_rel_from ON wiki_relations(from_page_id,status);
CREATE INDEX IF NOT EXISTS wiki_rel_to ON wiki_relations(to_page_id,status);

CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY, page_id TEXT NOT NULL UNIQUE REFERENCES wiki_pages(page_id),
  skill_id TEXT, skill_version TEXT, intent TEXT NOT NULL, task_signature_json TEXT NOT NULL,
  preconditions_json TEXT NOT NULL DEFAULT '[]', steps_json TEXT NOT NULL DEFAULT '[]',
  outcome_json TEXT NOT NULL, failure_patterns_json TEXT NOT NULL DEFAULT '[]',
  trace_ids_json TEXT NOT NULL DEFAULT '[]', confidence REAL NOT NULL DEFAULT 0,
  user_confirmed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'candidate',
  revision_id TEXT NOT NULL, previous_revision_id TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, last_used_at TEXT, use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS cases_skill_status ON cases(skill_id,status,confidence DESC);
CREATE INDEX IF NOT EXISTS cases_intent ON cases(intent,status);

CREATE TABLE IF NOT EXISTS skills (
  skill_id TEXT PRIMARY KEY, namespace TEXT NOT NULL DEFAULT 'workspace', name TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('builtin','workspace','entrypoint','mcp')),
  source_path TEXT, current_revision_id TEXT REFERENCES skill_revisions(revision_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
  current_version TEXT, status TEXT NOT NULL DEFAULT 'active', description TEXT NOT NULL DEFAULT '',
  tool_policy_json TEXT NOT NULL DEFAULT '{}', references_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(namespace,name)
);
CREATE INDEX IF NOT EXISTS skills_catalog ON skills(namespace,status,name);

CREATE TABLE IF NOT EXISTS skill_revisions (
  revision_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skills(skill_id),
  skill_version TEXT NOT NULL, content_hash TEXT NOT NULL, content TEXT NOT NULL,
  previous_revision_id TEXT REFERENCES skill_revisions(revision_id), supersedes_revision_id TEXT,
  source_case_ids_json TEXT NOT NULL DEFAULT '[]', eval_pack_id TEXT REFERENCES eval_packs(eval_pack_id)
    ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED,
  author_actor TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'staging', created_at TEXT NOT NULL,
  UNIQUE(skill_id,skill_version), UNIQUE(skill_id,content_hash)
);
CREATE INDEX IF NOT EXISTS skill_revision_current ON skill_revisions(skill_id,status,created_at DESC);

CREATE TABLE IF NOT EXISTS trace_index (
  trace_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, root_run_id TEXT, session_key TEXT,
  started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT NOT NULL DEFAULT 'unknown',
  event_count INTEGER NOT NULL DEFAULT 0, tool_count INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '', summary_hash TEXT, redaction_version TEXT NOT NULL,
  event_cursor TEXT, payload_expire_at TEXT, trace_expire_at TEXT, source_path TEXT NOT NULL,
  index_status TEXT NOT NULL DEFAULT 'indexed', last_error TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trace_session_time ON trace_index(session_key,started_at DESC);
CREATE INDEX IF NOT EXISTS trace_outcome_time ON trace_index(outcome,started_at DESC);
CREATE INDEX IF NOT EXISTS trace_expiry ON trace_index(trace_expire_at,payload_expire_at);

CREATE TABLE IF NOT EXISTS eval_packs (
  eval_pack_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skills(skill_id),
  skill_revision_id TEXT NOT NULL REFERENCES skill_revisions(revision_id), dataset_hash TEXT NOT NULL,
  fixture_hash TEXT NOT NULL, rubric_json TEXT NOT NULL, split_policy_json TEXT NOT NULL,
  source_manifest_json TEXT NOT NULL, question_count INTEGER NOT NULL, valid_count INTEGER NOT NULL,
  evidence_grade TEXT NOT NULL CHECK(evidence_grade IN ('standard','limited','insufficient_evidence')),
  status TEXT NOT NULL DEFAULT 'draft', sealed_by TEXT, sealed_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(skill_revision_id,dataset_hash)
);
CREATE INDEX IF NOT EXISTS eval_pack_skill ON eval_packs(skill_id,status,created_at DESC);

CREATE TABLE IF NOT EXISTS eval_runs (
  eval_run_id TEXT PRIMARY KEY, eval_pack_id TEXT NOT NULL REFERENCES eval_packs(eval_pack_id),
  baseline_revision_id TEXT NOT NULL REFERENCES skill_revisions(revision_id) ON DELETE RESTRICT,
  candidate_revision_id TEXT REFERENCES skill_revisions(revision_id) ON DELETE RESTRICT,
  baseline_hash TEXT NOT NULL, candidate_hash TEXT, model_id TEXT NOT NULL,
  tool_schema_digest TEXT NOT NULL, fixture_hash TEXT NOT NULL, dataset_hash TEXT NOT NULL,
  seed TEXT NOT NULL, replay_group_id TEXT NOT NULL, replay_attempt INTEGER NOT NULL DEFAULT 1,
  evidence_grade TEXT NOT NULL CHECK(evidence_grade IN ('standard','limited','insufficient_evidence')),
  consistency_result TEXT NOT NULL DEFAULT 'not_checked', is_independent_replay INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running', gate_result TEXT, metrics_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL, ended_at TEXT, error_message TEXT
);
CREATE INDEX IF NOT EXISTS eval_run_pack ON eval_runs(eval_pack_id,started_at DESC);

CREATE TABLE IF NOT EXISTS eval_case_results (
  eval_run_id TEXT NOT NULL REFERENCES eval_runs(eval_run_id), case_key TEXT NOT NULL,
  split TEXT NOT NULL CHECK(split IN ('train','validation','holdout')), outcome TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, latency_ms REAL NOT NULL DEFAULT 0,
  tools_json TEXT NOT NULL DEFAULT '[]', high_risk_tool_count INTEGER NOT NULL DEFAULT 0,
  security_violation INTEGER NOT NULL DEFAULT 0, judge_actor TEXT NOT NULL,
  rationale TEXT NOT NULL DEFAULT '', response_digest TEXT, PRIMARY KEY(eval_run_id,case_key)
);
CREATE INDEX IF NOT EXISTS eval_results_split ON eval_case_results(eval_run_id,split,outcome);

CREATE TABLE IF NOT EXISTS maintenance_jobs (
  job_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, session_key TEXT NOT NULL,
  activity_epoch INTEGER NOT NULL, last_activity_at TEXT NOT NULL, last_message_cursor TEXT NOT NULL,
  last_review_cursor TEXT NOT NULL, snapshot_cursor TEXT NOT NULL, due_at TEXT NOT NULL,
  lease_until TEXT, status TEXT NOT NULL DEFAULT 'scheduled', retry_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT, worker_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(workspace,session_key)
);
CREATE INDEX IF NOT EXISTS maintenance_due ON maintenance_jobs(status,due_at);

CREATE TABLE IF NOT EXISTS maintenance_lock (
  workspace TEXT PRIMARY KEY, owner TEXT NOT NULL, lease_until TEXT NOT NULL,
  acquired_at TEXT NOT NULL, renewed_at TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS maintenance_lock_expiry ON maintenance_lock(lease_until);

CREATE TABLE IF NOT EXISTS tombstones (
  tombstone_id TEXT PRIMARY KEY, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
  content_hash TEXT NOT NULL, reason_code TEXT NOT NULL, requested_by TEXT NOT NULL,
  deleted_at TEXT NOT NULL, expires_at TEXT, source_revision_id TEXT,
  UNIQUE(object_type,object_id)
);
CREATE INDEX IF NOT EXISTS tombstone_hash ON tombstones(object_type,content_hash);

CREATE TABLE IF NOT EXISTS retrieval_events (
  retrieval_id TEXT PRIMARY KEY, trace_id TEXT, turn_id TEXT, session_key TEXT NOT NULL,
  intent TEXT NOT NULL, scopes_json TEXT NOT NULL, query_digest TEXT NOT NULL,
  result_ids_json TEXT NOT NULL, injected_ids_json TEXT NOT NULL DEFAULT '[]',
  injected_context_digest TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS retrieval_session_time ON retrieval_events(session_key,created_at DESC);

CREATE TABLE IF NOT EXISTS memory_outbox (
  outbox_id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
  object_type TEXT NOT NULL CHECK(object_type IN ('memory','wiki_page','case','skill','trace_summary')),
  object_id TEXT NOT NULL, revision_id TEXT, content_hash TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('upsert','archive','tombstone','rebuild')),
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK(status IN ('pending','leased','retry_wait','done','dead_letter','superseded')),
  attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, lease_until TEXT,
  last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(object_type,object_id,content_hash,operation)
);
CREATE INDEX IF NOT EXISTS memory_outbox_due ON memory_outbox(workspace,status,next_attempt_at);
CREATE INDEX IF NOT EXISTS memory_outbox_lease ON memory_outbox(workspace,lease_until);
CREATE INDEX IF NOT EXISTS memory_outbox_object ON memory_outbox(object_type,object_id,created_at DESC);
