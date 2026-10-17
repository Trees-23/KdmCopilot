"""Phase 0 schema, path and foreign-key tests."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from nanobot.memory.db import (
    MemoryWorkspaceError,
    connect_memory_db,
    connect_memory_db_path,
    resolve_memory_db_path,
)
from nanobot.memory.migrations.runner import apply_migrations


def _db() -> sqlite3.Connection:
    connection = connect_memory_db_path(":memory:")
    apply_migrations(connection)
    return connection


def test_workspace_path_is_local_and_connection_pragmas_are_per_connection(tmp_path: Path) -> None:
    database = resolve_memory_db_path(tmp_path)
    assert database == tmp_path.resolve() / ".nanobot" / "memory.sqlite3"
    if os.name == "posix":
        assert database.parent.stat().st_mode & 0o777 == 0o700
    first = connect_memory_db(tmp_path)
    second = connect_memory_db(tmp_path)
    try:
        assert first.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert first.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("workspace", ["", "/path/that/does/not/exist"])
def test_invalid_workspace_is_rejected(workspace: str) -> None:
    with pytest.raises(MemoryWorkspaceError):
        resolve_memory_db_path(workspace)


def test_all_required_tables_and_schema_meta_are_applied() -> None:
    connection = _db()
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert {
        "wiki_pages",
        "wiki_relations",
        "cases",
        "skills",
        "skill_revisions",
        "eval_packs",
        "eval_runs",
        "eval_case_results",
        "maintenance_jobs",
        "maintenance_lock",
        "tombstones",
        "retrieval_events",
        "memory_outbox",
        "phase6_alert_deliveries",
        "semantic_task_evidence",
        "semantic_quality_reviews",
        "ab_evaluations",
        "ab_case_results",
        "schema_meta",
    } <= tables
    assert connection.execute("SELECT status FROM schema_meta WHERE version=1").fetchone()[0] == "applied"


def test_skills_and_skill_revisions_foreign_keys() -> None:
    connection = _db()
    connection.execute(
        "INSERT INTO skills(skill_id,name,source_kind,created_at,updated_at) "
        "VALUES('s1','skill','workspace','now','now')"
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES('sr1','s1','1','sha256:s','body','test','now')"
    )
    connection.execute("UPDATE skills SET current_revision_id='sr1' WHERE skill_id='s1'")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
            "VALUES('bad','missing','1','sha256:b','body','test','now')"
        )


def test_eval_foreign_keys_reject_missing_revisions() -> None:
    connection = _db()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO eval_runs(eval_run_id,eval_pack_id,baseline_revision_id,baseline_hash,model_id,"
            "tool_schema_digest,fixture_hash,dataset_hash,seed,replay_group_id,started_at) "
            "VALUES('run','missing','missing','sha256:b','model','sha256:t','sha256:f','sha256:d','seed','group','now')"
        )
