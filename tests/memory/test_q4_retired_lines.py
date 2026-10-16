"""Q4 historical upgrade, restart and retained runtime boundaries."""

import hashlib
import json
import sqlite3

from nanobot.config.schema import Phase6Config, Phase6EvolutionConfig
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.memory.continuous import Phase6RuntimeConfig, collect_capacity_metrics
from nanobot.memory.intent import IntentRouter
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.policy import MemoryScopePolicy
from nanobot.memory.retriever import MemoryRetriever
from nanobot.memory.schema import MIGRATIONS_DIR
from tests.memory.legacy_schema import open_pre_q4_db
from tests.memory.test_asset_retrieval import _asset
from tests.memory.test_legacy_purge import _row

RETIRED = {"failure_issue_candidates", "failure_issue_actions", "failure_issue_deliveries",
           "failure_issues", "recovery_case_reviews", "trace_index", "memory_records", "memory_revisions"}


def test_populated_old_31_upgrade_preserves_active_data_hashes_and_does_not_resurrect(tmp_path):
    c = open_pre_q4_db(tmp_path)
    _row(c, "failure_issues", status="pending_review")
    _row(c, "failure_issue_candidates", status="queued")
    _row(c, "failure_issue_actions", action="note")
    _row(c, "failure_issue_deliveries", status="sent")
    _row(c, "recovery_case_reviews", status="passed")
    _row(c, "trace_index")
    _row(c, "memory_records", memory_type="fact")
    _row(c, "memory_revisions")
    c.execute("UPDATE memory_records SET current_revision_id='fixture'")
    _row(c, "recovery_episodes", redaction_status="safe", status="failed")
    _row(c, "memory_outbox", object_type="skill", operation="upsert", status="done")
    c.execute("CREATE TABLE data_purge_completions(purge_id TEXT PRIMARY KEY,completed_at TEXT NOT NULL,plan_digest TEXT NOT NULL)")
    c.executemany("INSERT INTO data_purge_completions VALUES(?,'now','digest')", [("self-evolution-p6-v1",), ("legacy-maintenance-cp6-v1",)])
    c.commit()
    _asset(c, tmp_path, "retained", injection_count=2, post_injection_success_count=1)
    old_meta = [tuple(row) for row in c.execute("SELECT * FROM schema_meta ORDER BY version")]
    kept = {table: [tuple(r) for r in c.execute('SELECT * FROM ' + table)] for table in
            ("memory_assets", "recovery_episodes", "memory_outbox", "data_purge_completions")}
    c.close()
    # The deployment's actual opener applies Q4 without any old repair hook.
    from nanobot.memory.maintenance import open_maintenance_db
    c = open_maintenance_db(tmp_path)
    assert not RETIRED & {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert all(tuple(row) in [tuple(r) for r in c.execute('SELECT * FROM schema_meta')] for row in old_meta)
    for table, rows in kept.items():
        assert [tuple(r) for r in c.execute('SELECT * FROM ' + table)] == rows
    before = list(c.iterdump())
    apply_migrations(c)
    assert list(c.iterdump()) == before
    assert not c.execute("PRAGMA foreign_key_check").fetchall()
    c.close()


def test_fresh_replay_and_restart_keep_historical_sql_hashes(tmp_path):
    from nanobot.memory.maintenance import open_maintenance_db
    c = open_maintenance_db(tmp_path)
    for path in MIGRATIONS_DIR.glob('*.sql'):
        row = c.execute('SELECT schema_hash,status FROM schema_meta WHERE version=?', (int(path.name[:4]),)).fetchone()
        assert tuple(row) == ('sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(), 'applied')
    assert not RETIRED & {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    apply_migrations(c)
    assert not c.execute('PRAGMA foreign_key_check').fetchall()
    assert c.execute('SELECT count(*) FROM memory_assets').fetchone()[0] == 0
    c.close()


def test_retrieval_has_no_retired_scopes_or_table_reads(tmp_path):
    from nanobot.memory.maintenance import open_maintenance_db
    c = open_maintenance_db(tmp_path)
    assert MemoryScopePolicy._SCOPES == frozenset({'asset', 'skill'})
    assert IntentRouter().classify('show latest trace').intent == 'task'
    assert IntentRouter().classify('show history').scopes == ('asset',)
    seen = []

    def authorize(action, table, *args):
        if action == sqlite3.SQLITE_READ:
            seen.append(table)
            if table in RETIRED:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    c.set_authorizer(authorize)
    router = IntentRouter()
    scope = MemoryScopePolicy().for_intent(router.classify('ordinary task'))
    assert MemoryRetriever()._search(c, tmp_path, 'query', scope, 10, '') == []
    metrics = collect_capacity_metrics(c)
    assert 'memory_assets' in metrics and 'evolution_candidate_staging' in metrics
    assert not set(seen) & RETIRED
    c.close()


def test_retired_config_fields_and_pipeline_switches_are_absent():
    assert not {'recovery_review_enabled', 'recovery_notifications_enabled', 'recovery_skill_candidate_enabled'} & Phase6EvolutionConfig.model_fields.keys()
    assert not {'retention_days', 'payload_retention_days'} & Phase6Config.model_fields.keys()
    assert not {'retention_days', 'payload_retention_days'} & Phase6RuntimeConfig.__dataclass_fields__.keys()
    assert 'notification_groups' in Phase6EvolutionConfig.model_fields


def test_retiring_old_system_job_is_durable_but_cannot_remove_an_agent_job(tmp_path):
    path = tmp_path / 'cron.json'
    service = CronService(path)
    job = CronJob(id='phase6-recovery-issue-review', name='phase6-recovery-issue-review', schedule=CronSchedule(kind='cron', expr='0 12 * * *'), payload=CronPayload(kind='system_event'))
    service.register_system_job(job)
    assert service.remove_job(job.id) == 'protected'
    assert service.unregister_system_job(job.id)
    assert not service.unregister_system_job(job.id)
    assert not any(j['id'] == job.id for j in json.loads(path.read_text())['jobs'])
    job.payload.kind = 'agent_turn'
    service.register_system_job(job)
    assert not service.unregister_system_job(job.id)
