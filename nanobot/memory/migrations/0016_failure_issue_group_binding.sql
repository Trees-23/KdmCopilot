-- Repair historical databases whose v14 metadata was recorded before the
-- Issue group binding column reached the physical table.
CREATE INDEX IF NOT EXISTS failure_issues_group_status_v16
  ON failure_issues(workspace,group_openid,status,created_at DESC);
