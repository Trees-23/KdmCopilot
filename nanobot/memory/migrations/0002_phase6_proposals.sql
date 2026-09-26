CREATE TABLE IF NOT EXISTS skill_proposals (
  proposal_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  skill_id TEXT,
  skill_name TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('workspace','builtin','shared','entrypoint','mcp')),
  target TEXT NOT NULL CHECK(target IN ('workspace_adopt_proposal','git_pr_proposal')),
  baseline_revision_id TEXT,
  candidate_revision_id TEXT,
  baseline_hash TEXT NOT NULL,
  candidate_hash TEXT NOT NULL,
  version_epoch INTEGER NOT NULL DEFAULT 0,
  gate_result TEXT NOT NULL CHECK(gate_result IN ('passed','failed','insufficient_evidence','stale')),
  gate_snapshot_json TEXT NOT NULL DEFAULT '{}',
  trace_ids_json TEXT NOT NULL DEFAULT '[]',
  case_ids_json TEXT NOT NULL DEFAULT '[]',
  eval_run_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL CHECK(status IN (
    'draft','evaluating','rejected_by_gate','insufficient_evidence','stale',
    'eligible_for_confirmation','notified','approved','adopting','adopted',
    'creating_pr','pr_created','publish_approved','publishing','published',
    'rejected_by_admin','expired','failed','rolled_back'
  )),
  confirmation_code_hash TEXT,
  confirmation_expires_at TEXT,
  approved_by TEXT,
  approved_at TEXT,
  rejected_by TEXT,
  rejected_at TEXT,
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(skill_name,source_kind,baseline_hash,candidate_hash)
);
CREATE INDEX IF NOT EXISTS skill_proposals_status ON skill_proposals(workspace,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS skill_proposals_skill ON skill_proposals(workspace,skill_name,status);

CREATE TABLE IF NOT EXISTS proposal_deliveries (
  delivery_id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL REFERENCES skill_proposals(proposal_id) ON DELETE CASCADE,
  workspace TEXT NOT NULL,
  group_openid TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','leased','sent','retry_wait','dead_letter','superseded')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  lease_until TEXT,
  provider_message_id TEXT,
  last_error TEXT,
  audit_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(proposal_id,group_openid,content_hash)
);
CREATE INDEX IF NOT EXISTS proposal_deliveries_due ON proposal_deliveries(workspace,status,next_attempt_at);

CREATE TABLE IF NOT EXISTS proposal_actions (
  action_id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL REFERENCES skill_proposals(proposal_id) ON DELETE CASCADE,
  workspace TEXT NOT NULL,
  action TEXT NOT NULL,
  actor_openid TEXT NOT NULL,
  group_openid TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_digest TEXT NOT NULL,
  result_status TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS proposal_actions_proposal ON proposal_actions(proposal_id,created_at);

CREATE TABLE IF NOT EXISTS group_memory_scopes (
  group_openid TEXT PRIMARY KEY,
  namespace TEXT NOT NULL,
  observation_enabled INTEGER NOT NULL DEFAULT 0 CHECK(observation_enabled IN (0,1)),
  notification_enabled INTEGER NOT NULL DEFAULT 0 CHECK(notification_enabled IN (0,1)),
  command_require_mention INTEGER NOT NULL DEFAULT 1 CHECK(command_require_mention IN (0,1)),
  daily_notification_limit INTEGER NOT NULL DEFAULT 3 CHECK(daily_notification_limit > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
