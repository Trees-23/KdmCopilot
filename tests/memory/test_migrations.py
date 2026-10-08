"""Migration idempotency and failure recovery tests."""

from __future__ import annotations

import pytest

from nanobot.memory.db import connect_memory_db_path
from nanobot.memory.migrations.runner import MigrationError, apply_migrations


def test_migration_is_idempotent_and_schema_hash_stable() -> None:
    connection = connect_memory_db_path(":memory:")
    apply_migrations(connection)
    first = dict(connection.execute("SELECT * FROM schema_meta WHERE version=1").fetchone())
    apply_migrations(connection)
    second = dict(connection.execute("SELECT * FROM schema_meta WHERE version=1").fetchone())
    assert first["schema_hash"] == second["schema_hash"]
    assert second["status"] == "applied"


def test_failed_migration_is_recorded_and_can_be_recovered() -> None:
    connection = connect_memory_db_path(":memory:")
    with pytest.raises(MigrationError):
        apply_migrations(connection, sql="CREATE TABLE partial(id INTEGER); THIS IS NOT VALID;")
    failed = connection.execute("SELECT status,error_message FROM schema_meta WHERE version=1").fetchone()
    assert failed[0] == "failed"
    assert failed[1]
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partial'"
    ).fetchone() is None
    apply_migrations(connection)
    assert connection.execute("SELECT status FROM schema_meta WHERE version=1").fetchone()[0] == "applied"


def test_latest_migration_repairs_missing_failure_issue_group_binding() -> None:
    connection = connect_memory_db_path(":memory:")
    apply_migrations(connection)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("ALTER TABLE failure_issues RENAME TO failure_issues_before_group_repair")
    connection.execute(
        """CREATE TABLE failure_issues (
        issue_id TEXT PRIMARY KEY,workspace TEXT NOT NULL,recovery_key TEXT NOT NULL,
        episode_ids_json TEXT NOT NULL,title TEXT NOT NULL,task_goal TEXT,failure_class TEXT NOT NULL,
        correction_goal TEXT,summary TEXT NOT NULL,status TEXT NOT NULL,recommendation TEXT NOT NULL,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,reviewed_at TEXT,reviewed_by TEXT,review_reason TEXT)"""
    )
    connection.execute(
        """INSERT INTO failure_issues
        SELECT issue_id,workspace,recovery_key,episode_ids_json,title,task_goal,failure_class,
        correction_goal,summary,status,recommendation,created_at,updated_at,reviewed_at,reviewed_by,
        review_reason FROM failure_issues_before_group_repair"""
    )
    connection.execute("DROP TABLE failure_issues_before_group_repair")
    connection.execute("DELETE FROM schema_meta WHERE version=16")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.commit()

    apply_migrations(connection)

    assert "group_openid" in {row[1] for row in connection.execute("PRAGMA table_info(failure_issues)")}
    assert connection.execute("SELECT status FROM schema_meta WHERE version=16").fetchone()[0] == "applied"
