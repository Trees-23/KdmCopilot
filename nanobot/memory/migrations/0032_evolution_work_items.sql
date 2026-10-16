-- Gateway writes durable triggers. The same-image worker leases execution.
CREATE TABLE evolution_work_items (
  work_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('review','lesson')),
  trigger_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
  generation INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at TEXT,
  available_at TEXT NOT NULL,
  error_code TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace,kind,trigger_id),
  CHECK((status='running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status<>'running' AND lease_owner IS NULL AND lease_expires_at IS NULL))
);
CREATE INDEX evolution_work_due ON evolution_work_items(workspace,status,available_at);
