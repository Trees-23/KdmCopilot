CREATE TABLE IF NOT EXISTS phase6_alert_deliveries (
  alert_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  group_openid TEXT NOT NULL,
  category TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','leased','sent','retry_wait','dead_letter')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  lease_until TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace,group_openid,category,fingerprint)
);
CREATE INDEX IF NOT EXISTS phase6_alert_deliveries_due
  ON phase6_alert_deliveries(workspace,status,next_attempt_at);
