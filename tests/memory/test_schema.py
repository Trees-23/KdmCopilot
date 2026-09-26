"""Phase 0 schema, path, foreign-key and FTS tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import nanobot.memory.index as memory_index
from nanobot.memory.db import (
    MemoryWorkspaceError,
    connect_memory_db,
    connect_memory_db_path,
    foreign_keys_enabled,
    resolve_memory_db_path,
)
from nanobot.memory.index import initialize_fts, rebuild_fts, search_memory
from nanobot.memory.migrations.runner import apply_migrations


def _db() -> sqlite3.Connection:
    connection = connect_memory_db_path(":memory:")
    apply_migrations(connection)
    return connection


def test_workspace_path_is_local_and_connection_pragmas_are_per_connection(tmp_path: Path) -> None:
    database = resolve_memory_db_path(tmp_path)
    assert database == tmp_path.resolve() / ".nanobot" / "memory.sqlite3"
    assert database.parent.stat().st_mode & 0o777 == 0o700
    first = connect_memory_db(tmp_path)
    second = connect_memory_db(tmp_path)
    try:
        assert foreign_keys_enabled(first)
        assert foreign_keys_enabled(second)
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
        "memory_records",
        "memory_revisions",
        "wiki_pages",
        "wiki_relations",
        "cases",
        "skills",
        "skill_revisions",
        "trace_index",
        "eval_packs",
        "eval_runs",
        "eval_case_results",
        "maintenance_jobs",
        "maintenance_lock",
        "tombstones",
        "retrieval_events",
        "memory_outbox",
        "schema_meta",
    } <= tables
    assert connection.execute("SELECT status FROM schema_meta WHERE version=1").fetchone()[0] == "applied"


def test_memory_revision_foreign_keys_and_deferred_current_pointer() -> None:
    connection = _db()
    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO memory_records(memory_id,memory_type,source_actor,created_at,updated_at,current_revision_id) "
        "VALUES('m1','fact','test','now','now','r1')"
    )
    connection.execute(
        "INSERT INTO memory_revisions(revision_id,memory_id,revision_no,content,content_hash,author_actor,reason,created_at) "
        "VALUES('r1','m1',1,'body','sha256:x','test','test','now')"
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO memory_revisions(revision_id,memory_id,revision_no,content,content_hash,author_actor,reason,created_at) "
            "VALUES('bad','missing',1,'body','sha256:y','test','test','now')"
        )


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


def test_fts_initialization_rebuild_and_active_filtering() -> None:
    connection = _db()
    status = initialize_fts(connection)
    assert status.available is True
    assert initialize_fts(connection).available is True
    connection.execute(
        "INSERT INTO memory_records(memory_id,memory_type,source_actor,title,summary,status,created_at,updated_at) "
        "VALUES('active','fact','test','Active memory','summary','active','now','now')"
    )
    connection.execute(
        "INSERT INTO memory_revisions(revision_id,memory_id,revision_no,content,content_hash,author_actor,reason,created_at) "
        "VALUES('active-r','active',1,'important keyword','sha256:a','test','test','now')"
    )
    connection.execute("UPDATE memory_records SET current_revision_id='active-r' WHERE memory_id='active'")
    connection.execute(
        "INSERT INTO memory_records(memory_id,memory_type,source_actor,title,status,created_at,updated_at) "
        "VALUES('archived','fact','test','Archived keyword','archived','now','now')"
    )
    connection.execute(
        "INSERT INTO memory_revisions(revision_id,memory_id,revision_no,content,content_hash,author_actor,reason,created_at) "
        "VALUES('archived-r','archived',1,'keyword','sha256:b','test','test','now')"
    )
    connection.execute("UPDATE memory_records SET current_revision_id='archived-r' WHERE memory_id='archived'")
    connection.execute(
        "INSERT INTO tombstones(tombstone_id,object_type,object_id,content_hash,reason_code,requested_by,deleted_at) "
        "VALUES('t1','memory','archived','sha256:b','test','test','now')"
    )
    connection.commit()
    assert rebuild_fts(connection) == 1
    assert [row["object_id"] for row in search_memory(connection, "keyword")] == ["active"]


def test_fts_detection_reports_degraded_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingConnection:
        def execute(self, statement: str, parameters: tuple[str, ...] = ()):
            if "sqlite_version" in statement:
                return type("Result", (), {"fetchone": lambda self: ("3.0.0",)})()
            raise sqlite3.OperationalError("fts5 unavailable")

        def close(self) -> None:
            pass

    monkeypatch.setattr(memory_index.sqlite3, "connect", lambda _: FailingConnection())
    status = memory_index.detect_fts5()
    assert status.available is False
    assert status.error == "fts5 unavailable"
