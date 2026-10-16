"""Real-schema P6 offline cleanup, rollback and once-only boundaries."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from nanobot.cli.memory import create_memory_app
from nanobot.memory.legacy_purge import (
    MARKER_TABLE,
    PURGE_TARGETS,
    PurgeError,
    _purge,
    preview_legacy_purge,
    purge_legacy_data,
)
from nanobot.memory.maintenance import open_maintenance_db
from tests.memory.test_asset_retrieval import _asset


def _row(connection, table, **extra):
    """Fill uninteresting required fields; callers specify checks and FK identities."""
    values = {}
    for _, name, kind, required, default, pk in connection.execute(f"PRAGMA table_info({table})"):
        if default is None and (required or pk):
            values[name] = 0 if kind == "INTEGER" else "fixture"
            if name.endswith("_json"):
                values[name] = "[]"
    values.update(extra)
    connection.execute(
        f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


@pytest.fixture
def populated(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _row(connection, "skills", skill_id="skill:m19:fixture", source_kind="workspace")
    _row(connection, "skill_revisions", skill_id="skill:m19:fixture")
    connection.execute("UPDATE skills SET current_revision_id='fixture'")
    for i, skill_id in enumerate(("skill:recovery-fixture", "skill:evolution-fixture", "skill-overlay-hot-reload-m9", "manual")):
        _row(connection, "skills", skill_id=skill_id, name=f"skill{i}", source_kind="workspace")
        _row(connection, "skill_revisions", revision_id=f"revision{i}", skill_id=skill_id)
        connection.execute("UPDATE skills SET current_revision_id=? WHERE skill_id=?", (f"revision{i}", skill_id))
    _row(connection, "evolution_task_evidence", qualification="partial_evidence", redaction_status="safe")
    _row(connection, "evolution_step_evidence", risk_level="R0", candidate_use="reusable", redaction_status="safe")
    _row(connection, "evolution_task_links")
    _row(connection, "evolution_candidate_staging", source_kind="skill_candidate", status="queued")
    _row(connection, "recovery_episodes", redaction_status="safe", status="recovered")
    _row(connection, "recovery_case_reviews", status="passed")
    _row(connection, "failure_issues", status="pending_review")
    _row(connection, "failure_issue_candidates", status="queued")
    _row(connection, "failure_issue_actions", action="note")
    _row(connection, "failure_issue_deliveries", status="sent")
    _row(connection, "semantic_task_evidence", redaction_status="safe", evidence_status="eligible")
    _row(connection, "semantic_quality_reviews", status="passed")
    _row(connection, "ab_evaluations", mode="enforced", status="passed")
    _row(connection, "ab_case_results", split="positive", judge_status="passed")
    _row(connection, "evaluation_candidate_admissions")
    _row(connection, "evaluation_model_call_reservations", status="settled")
    _row(connection, "eval_packs", skill_id="skill:m19:fixture", evidence_grade="standard")
    _row(connection, "eval_runs", evidence_grade="standard")
    _row(connection, "eval_case_results", split="train")
    _row(connection, "eval_cases", split="train")
    _row(connection, "skill_proposals", source_kind="workspace", target="workspace_adopt_proposal", gate_result="passed", status="draft")
    _row(connection, "proposal_actions")
    _row(connection, "proposal_deliveries", status="sent")
    _row(connection, "wiki_pages", page_type="case")
    _row(connection, "cases")
    _row(connection, "wiki_pages", page_id="keep", slug="keep", page_type="fact")
    _row(connection, "retrieval_events", outcome="indexed")
    # Non-P6 database rows and filesystem content must survive verbatim.
    _row(connection, "memory_records", memory_type="fact")
    _row(connection, "trace_index")
    _row(connection, "maintenance_jobs")
    expired = "2000-01-01T00:00:00+00:00"
    _row(connection, "maintenance_lock", lease_until=expired)
    connection.commit()
    for name, level, status in (("l1", "L1", "active"), ("l2", "L2", "archived"), ("tomb", "L1", "tombstone")):
        _asset(connection, tmp_path, name, level=level, status=status, injection_count=3, post_injection_success_count=2)
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    connection.close()
    return tmp_path / ".nanobot/memory.sqlite3"


def _dump(connection):
    return list(connection.iterdump())


def _execute(path, plan=None):
    return purge_legacy_data(path, expected_plan=plan or preview_legacy_purge(path)["plan_digest"], writers_stopped=True)


def test_dry_run_is_read_only_and_missing_path_does_not_create_database(populated, tmp_path):
    before = hashlib.sha256(populated.read_bytes()).hexdigest()
    with closing(sqlite3.connect(populated)) as connection, connection:
        content = _dump(connection)
    result = preview_legacy_purge(populated)
    assert result["status"] == "dry_run" and result["total_changes"] == 0
    assert {step["step"] for step in result["steps"]} == set(range(1, 24))
    assert all(step["rows"] > 0 for step in result["steps"])
    assert hashlib.sha256(populated.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(populated)) as connection, connection:
        assert _dump(connection) == content
    missing = tmp_path / "absent/nested/memory.sqlite3"
    with pytest.raises(FileNotFoundError):
        preview_legacy_purge(missing)
    assert not missing.parent.exists()


def test_full_cleanup_preserves_schema_manual_skills_and_non_p6_rows(populated):
    with closing(sqlite3.connect(populated)) as connection, connection:
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        preserved = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in
                     ("schema_meta", "maintenance_jobs", "maintenance_lock", "trace_index", "memory_records")}
        manual = connection.execute("SELECT * FROM skills WHERE skill_id='manual'").fetchall()
        manual_revision = connection.execute("SELECT * FROM skill_revisions WHERE skill_id='manual'").fetchall()
    assert _execute(populated)["status"] == "completed"
    with closing(sqlite3.connect(populated)) as connection, connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert all(step["rows"] == 0 for step in preview_legacy_purge(populated)["steps"])
        assert connection.execute(f"SELECT count(*) FROM {MARKER_TABLE}").fetchone()[0] == 1
        for table, rows in preserved.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert connection.execute("SELECT * FROM skills").fetchall() == manual
        assert connection.execute("SELECT * FROM skill_revisions").fetchall() == manual_revision
        assert connection.execute("SELECT page_id FROM wiki_pages").fetchall() == [("keep",)]
        current = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        assert all(row in current for row in schema)


@pytest.mark.parametrize("target", PURGE_TARGETS, ids=lambda target: f"{target.step}-{target.table}")
def test_failure_at_every_step_rolls_back_all_rows_and_restores_foreign_keys(populated, target):
    connection = sqlite3.connect(populated)
    connection.execute("PRAGMA foreign_keys=ON")
    operation = "UPDATE OF current_revision_id" if target.update else "DELETE"
    connection.execute(f"CREATE TRIGGER fail_p6 BEFORE {operation} ON {target.table} BEGIN SELECT RAISE(ABORT,'fixture'); END")
    connection.commit()
    before = _dump(connection)
    with pytest.raises(sqlite3.IntegrityError):
        _purge(connection, preview_legacy_purge(populated)["plan_digest"])
    assert not connection.in_transaction
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert _dump(connection) == before
    connection.execute("DROP TRIGGER fail_p6")
    connection.commit()
    connection.close()
    assert _execute(populated)["status"] == "completed"


def test_marker_insert_failure_rolls_back_ddl_and_deletes(populated):
    connection = sqlite3.connect(populated)
    connection.execute("PRAGMA foreign_keys=ON")
    before = _dump(connection)
    connection.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == MARKER_TABLE else sqlite3.SQLITE_OK)
    with pytest.raises(sqlite3.DatabaseError):
        _purge(connection, preview_legacy_purge(populated)["plan_digest"])
    connection.set_authorizer(None)
    assert _dump(connection) == before
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    connection.close()


def test_interrupt_after_deletes_restores_all_rows_and_foreign_keys(populated, monkeypatch):
    import nanobot.memory.legacy_purge as module

    expected = preview_legacy_purge(populated)["plan_digest"]
    original = module._plan
    calls = 0

    def interrupt_postcheck(connection):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(connection)

    connection = sqlite3.connect(populated)
    connection.execute("PRAGMA foreign_keys=ON")
    before = _dump(connection)
    monkeypatch.setattr(module, "_plan", interrupt_postcheck)
    with pytest.raises(KeyboardInterrupt):
        _purge(connection, expected)
    assert not connection.in_transaction
    assert _dump(connection) == before
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    connection.close()


def test_fk_violation_from_outside_scope_rejects_cleanup_without_expanding_scope(populated):
    with closing(sqlite3.connect(populated)) as connection, connection:
        _row(connection, "wiki_relations", from_page_id="fixture", to_page_id="keep")
    before = preview_legacy_purge(populated)
    with pytest.raises(PurgeError, match="外键违规"):
        _execute(populated)
    assert preview_legacy_purge(populated) == before
    with closing(sqlite3.connect(populated)) as connection, connection:
        assert connection.execute("SELECT count(*) FROM wiki_relations").fetchone()[0] == 1


def test_silent_delete_trigger_is_not_mistaken_for_success(populated):
    with closing(sqlite3.connect(populated)) as connection, connection:
        connection.execute("CREATE TRIGGER skip_p6 BEFORE DELETE ON memory_assets BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(PurgeError, match="仍有范围内数据"):
        _execute(populated)
    assert not preview_legacy_purge(populated)["completed"]


def test_successful_rerun_keeps_new_sources_assets_events_and_counts(populated):
    old_plan = preview_legacy_purge(populated)["plan_digest"]
    _execute(populated, old_plan)
    connection = open_maintenance_db(populated.parent.parent)
    _row(connection, "evolution_task_evidence", qualification="partial_evidence", redaction_status="safe")
    _row(connection, "retrieval_events", outcome="indexed")
    connection.commit()
    _asset(connection, populated.parent.parent, "new-l1", injection_count=2, post_injection_success_count=1)
    _asset(connection, populated.parent.parent, "new-l2", level="L2")
    before = _dump(connection)
    connection.close()
    assert _execute(populated, old_plan)["status"] == "already_completed"
    with closing(sqlite3.connect(populated)) as connection, connection:
        assert _dump(connection) == before
    # A later Q4 dropping legacy tables must not reactivate the once-only purge.
    with closing(sqlite3.connect(populated)) as connection, connection:
        connection.execute("DROP TABLE failure_issue_candidates")
    assert _execute(populated, old_plan)["status"] == "already_completed"
    assert preview_legacy_purge(populated)["completed"]


@pytest.mark.parametrize("table,column,where", [
    ("maintenance_lock", "lease_until", "1"),
    ("maintenance_jobs", "lease_until", "1"),
    ("evolution_candidate_staging", "lease_expires_at", "1"),
    ("ab_evaluations", "execution_deadline", "1"),
])
def test_active_offline_model_or_maintenance_lease_is_rejected(populated, table, column, where):
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    with closing(sqlite3.connect(populated)) as connection, connection:
        if table == "evolution_candidate_staging":
            connection.execute("UPDATE evolution_candidate_staging SET status='evaluating',lease_owner='worker',lease_expires_at=?", (future,))
        elif table == "ab_evaluations":
            connection.execute("UPDATE ab_evaluations SET status='running',execution_deadline=?", (future,))
        else:
            connection.execute(f"UPDATE {table} SET {column}=? WHERE {where}", (future,))
    with pytest.raises(PurgeError, match="仍有有效租约"):
        _execute(populated)
    assert not preview_legacy_purge(populated)["completed"]


def test_online_wal_connection_cannot_be_purged(populated, monkeypatch):
    import nanobot.memory.legacy_purge as module

    plan = preview_legacy_purge(populated)["plan_digest"]
    connection = sqlite3.connect(populated)
    connection.execute("SELECT count(*) FROM memory_assets").fetchone()
    original = module._connect

    def short_timeout(*args, **kwargs):
        result = original(*args, **kwargs)
        result.execute("PRAGMA busy_timeout=10")
        return result

    monkeypatch.setattr(module, "_connect", short_timeout)
    with pytest.raises(PurgeError, match="独占访问"):
        _execute(populated, plan)
    connection.close()
    assert not preview_legacy_purge(populated)["completed"]


def test_concurrent_invocations_recheck_marker_after_acquiring_lock(populated):
    plan = preview_legacy_purge(populated)["plan_digest"]
    barrier = Barrier(2)

    def run():
        barrier.wait()
        return _execute(populated, plan)["status"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        assert sorted(future.result() for future in futures) == ["already_completed", "completed"]


def test_separate_processes_serialize_before_opening_exclusive_wal_connections(populated):
    plan = preview_legacy_purge(populated)["plan_digest"]
    barrier = Barrier(2)
    script = (
        "import sys; from nanobot.memory.legacy_purge import purge_legacy_data; "
        "print(purge_legacy_data(sys.argv[1], expected_plan=sys.argv[2], writers_stopped=True)['status'])"
    )

    def run():
        barrier.wait()
        return subprocess.check_output([sys.executable, "-c", script, str(populated), plan], text=True).strip()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        assert sorted(future.result() for future in futures) == ["already_completed", "completed"]


def test_changed_plan_and_missing_preconditions_are_rejected(populated):
    plan = preview_legacy_purge(populated)["plan_digest"]
    with pytest.raises(PurgeError, match="停止所有写入"):
        purge_legacy_data(populated, expected_plan=plan, writers_stopped=False)
    with closing(sqlite3.connect(populated)) as connection, connection:
        _row(connection, "retrieval_events", retrieval_id="new", outcome="empty")
    with pytest.raises(PurgeError, match="清单已改变"):
        _execute(populated, plan)
    connection = sqlite3.connect(populated)
    with pytest.raises(PurgeError, match="空闲独立连接"):
        _purge(connection, plan)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN")
    with pytest.raises(PurgeError, match="空闲独立连接"):
        _purge(connection, plan)
    assert connection.in_transaction  # Never roll back somebody else's transaction.
    connection.close()


def test_cli_defaults_to_preview_and_requires_explicit_execute_preconditions(populated):
    app = typer.Typer()
    app.add_typer(create_memory_app(console=Console()), name="memory")
    runner = CliRunner()
    command = ["memory", "purge-legacy", "--database", str(populated)]
    result = runner.invoke(app, command)
    assert result.exit_code == 0 and json.loads(result.stdout)["status"] == "dry_run"
    assert runner.invoke(app, command + ["--execute"]).exit_code == 1
    assert runner.invoke(app, command + ["--execute", "--dry-run"]).exit_code == 2
    plan = preview_legacy_purge(populated)["plan_digest"]
    result = runner.invoke(app, command + ["--execute", "--writers-stopped", "--confirm-plan", plan])
    assert result.exit_code == 0 and json.loads(result.stdout)["status"] == "completed"
