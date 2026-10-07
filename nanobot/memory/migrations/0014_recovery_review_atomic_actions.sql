-- M17/M19: one-shot administrator revise/reject routing and group binding.
-- This migration is append-only. Existing action rows remain auditable.

ALTER TABLE failure_issues ADD COLUMN group_openid TEXT;

-- The original table predates the one-shot ``revise`` action and has a
-- restrictive CHECK constraint. Rebuild it while preserving every row.
CREATE TABLE failure_issue_actions_v14 (
  action_id TEXT PRIMARY KEY,
  issue_id TEXT NOT NULL REFERENCES failure_issues(issue_id) ON DELETE CASCADE,
  workspace TEXT NOT NULL,
  actor_openid TEXT NOT NULL,
  group_openid TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('note','record_case','record_memory','request_candidate','revise','reject')),
  note TEXT NOT NULL DEFAULT '',
  result_status TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_digest TEXT,
  direction_hash TEXT,
  request_id TEXT,
  candidate_id TEXT,
  created_at TEXT NOT NULL
);

INSERT INTO failure_issue_actions_v14
  (action_id,issue_id,workspace,actor_openid,group_openid,action,note,result_status,
   idempotency_key,created_at)
SELECT action_id,issue_id,workspace,actor_openid,group_openid,action,note,result_status,
       idempotency_key,created_at
  FROM failure_issue_actions;

DROP TABLE failure_issue_actions;
ALTER TABLE failure_issue_actions_v14 RENAME TO failure_issue_actions;

CREATE INDEX IF NOT EXISTS failure_issue_actions_issue
  ON failure_issue_actions(issue_id,created_at DESC);
CREATE INDEX IF NOT EXISTS failure_issue_actions_direction
  ON failure_issue_actions(issue_id,action,direction_hash,created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS failure_issue_revise_direction
  ON failure_issue_actions(issue_id,actor_openid,direction_hash)
  WHERE action='revise' AND direction_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS failure_issues_group_status
  ON failure_issues(workspace,group_openid,status,created_at DESC);
