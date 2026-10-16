"""Independent C-P6 scope, authorization preconditions and active lock preservation."""

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
from nanobot.memory.legacy_purge import MARKER_TABLE, PurgeError
from nanobot.memory.lock import acquire_lock, release_lock
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.maintenance_purge import (
    PURGE_ID,
    _purge_maintenance,
    preview_maintenance_purge,
    purge_maintenance_data,
)
from tests.memory.test_legacy_purge import _execute, _row


@pytest.fixture
def database(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _row(connection, "maintenance_jobs")
    _row(connection, "maintenance_lock", lease_until="2000-01-01T00:00:00+00:00")
    _row(connection, "memory_outbox", object_type="skill", operation="upsert", status="pending")
    connection.commit()
    connection.close()
    path = tmp_path / ".nanobot/memory.sqlite3"
    _execute(path)  # P6 leaves both maintenance rows and the outbox intact.
    return path


def _run(database, plan=None):
    return purge_maintenance_data(database, expected_plan=plan or preview_maintenance_purge(database)["plan_digest"], writers_stopped=True)


def test_preview_does_not_write_or_create_missing_database(database, tmp_path):
    with closing(sqlite3.connect(database)) as connection:
        before = list(connection.iterdump())
    result = preview_maintenance_purge(database)
    assert result["p6_completed"] and not result["completed"] and result["total_changes"] == 0
    assert [(s["table"], s["rows"]) for s in result["steps"]] == [("maintenance_jobs", 1), ("maintenance_lock", 1)]
    with closing(sqlite3.connect(database)) as connection:
        assert list(connection.iterdump()) == before
    missing = tmp_path / "missing/memory.sqlite3"
    with pytest.raises(FileNotFoundError):
        preview_maintenance_purge(missing)
    assert not missing.parent.exists()


def test_requires_p6_completion_and_offline_confirmation(tmp_path):
    open_maintenance_db(tmp_path).close()
    path = tmp_path / ".nanobot/memory.sqlite3"
    result = preview_maintenance_purge(path)
    assert not result["p6_completed"]
    with pytest.raises(PurgeError, match="先完成 P6"):
        _run(path)
    with pytest.raises(PurgeError, match="停止所有写入"):
        purge_maintenance_data(path, expected_plan=result["plan_digest"], writers_stopped=False)


@pytest.mark.parametrize("failure", ["maintenance_jobs", "maintenance_lock", "marker", "denied_marker"])
def test_delete_and_marker_failures_are_atomic(database, failure):
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    if failure in ("maintenance_jobs", "maintenance_lock"):
        connection.execute(f"CREATE TRIGGER fail_cp6 BEFORE DELETE ON {failure} BEGIN SELECT RAISE(ABORT,'fixture'); END")
    elif failure == "marker":
        connection.execute(f"CREATE TRIGGER fail_cp6 BEFORE INSERT ON {MARKER_TABLE} BEGIN SELECT RAISE(ABORT,'fixture'); END")
    connection.commit()
    before = list(connection.iterdump())
    if failure == "denied_marker":
        connection.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == MARKER_TABLE else sqlite3.SQLITE_OK)
    with pytest.raises(sqlite3.DatabaseError):
        _purge_maintenance(connection, preview_maintenance_purge(database)["plan_digest"])
    connection.set_authorizer(None)
    assert not connection.in_transaction
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert list(connection.iterdump()) == before
    connection.close()


def test_success_preserves_other_rows_and_rerun_keeps_new_jobs_and_active_lease(database):
    with closing(sqlite3.connect(database)) as connection:
        schemas = connection.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        marker = connection.execute(f"SELECT * FROM {MARKER_TABLE}").fetchall()
        outbox = connection.execute("SELECT * FROM memory_outbox").fetchall()
    old_plan = preview_maintenance_purge(database)["plan_digest"]
    assert _run(database, old_plan)["status"] == "completed"
    connection = open_maintenance_db(database.parent.parent)
    assert all(row in [tuple(value) for value in connection.execute("SELECT type,name,sql FROM sqlite_master")] for row in schemas)
    assert all(s["rows"] == 0 for s in preview_maintenance_purge(database)["steps"])
    assert connection.execute("SELECT * FROM memory_outbox").fetchall()[0][:] == outbox[0]
    assert connection.execute(f"SELECT * FROM {MARKER_TABLE} WHERE purge_id<>?", (PURGE_ID,)).fetchall()[0][:] == marker[0]
    _row(connection, "maintenance_jobs", job_id="new", session_key="new")
    connection.commit()
    lease = acquire_lock(connection, "fixture", "live-worker")
    assert lease is not None
    before = list(connection.iterdump())
    connection.close()
    assert _run(database, old_plan)["status"] == "already_completed"
    connection = open_maintenance_db(database.parent.parent)
    assert list(connection.iterdump()) == before
    assert release_lock(connection, lease)
    connection.close()


@pytest.mark.parametrize("table", ["maintenance_jobs", "maintenance_lock"])
def test_valid_lease_refuses_first_cleanup(database, table):
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f"UPDATE {table} SET lease_until=?", ((datetime.now(UTC) + timedelta(minutes=5)).isoformat(),))
    with pytest.raises(PurgeError, match="有效租约"):
        _run(database)
    assert not preview_maintenance_purge(database)["completed"]


def test_stale_preview_and_new_inbound_fk_refuse_cleanup(database):
    old_plan = preview_maintenance_purge(database)["plan_digest"]
    with closing(sqlite3.connect(database)) as connection, connection:
        _row(connection, "maintenance_jobs", job_id="new", session_key="new")
        connection.execute("CREATE TABLE protected(job_id TEXT REFERENCES maintenance_jobs(job_id))")
        connection.execute("INSERT INTO protected VALUES('fixture')")
    with pytest.raises(PurgeError, match="清单已改变"):
        _run(database, old_plan)
    with pytest.raises(sqlite3.IntegrityError):
        _run(database)
    assert not preview_maintenance_purge(database)["completed"]


def test_concurrent_processes_use_independent_marker_once(database):
    barrier = Barrier(2)
    plan = preview_maintenance_purge(database)["plan_digest"]
    script = "import sys; from nanobot.memory.maintenance_purge import purge_maintenance_data; print(purge_maintenance_data(sys.argv[1],expected_plan=sys.argv[2],writers_stopped=True)['status'])"

    def run():
        barrier.wait()
        return subprocess.check_output([sys.executable, "-c", script, str(database), plan], text=True).strip()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        assert sorted(f.result() for f in futures) == ["already_completed", "completed"]


def test_cli_defaults_to_read_only_and_requires_independent_execute(database):
    app = typer.Typer()
    app.add_typer(create_memory_app(console=Console()), name="memory")
    runner = CliRunner()
    command = ["memory", "purge-maintenance", "--database", str(database)]
    result = runner.invoke(app, command)
    assert result.exit_code == 0 and json.loads(result.stdout)["status"] == "dry_run"
    assert runner.invoke(app, command + ["--execute"]).exit_code == 1
    assert runner.invoke(app, command + ["--execute", "--dry-run"]).exit_code == 2
    plan = preview_maintenance_purge(database)["plan_digest"]
    result = runner.invoke(app, command + ["--execute", "--writers-stopped", "--confirm-plan", plan])
    assert result.exit_code == 0 and json.loads(result.stdout)["status"] == "completed"
