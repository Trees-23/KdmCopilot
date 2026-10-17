"""End-to-end Phase 0 persistence bootstrap in an isolated workspace."""

from __future__ import annotations

from pathlib import Path

from nanobot.memory.db import connect_memory_db, resolve_memory_db_path
from nanobot.memory.lock import acquire_lock, release_lock
from nanobot.memory.migrations.runner import apply_migrations, verify_schema


def test_phase0_bootstrap_is_repeatable_and_isolated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database_path = resolve_memory_db_path(workspace)
    connection = connect_memory_db(workspace)
    apply_migrations(connection)
    verify_schema(connection)
    assert database_path.exists()
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    lease = acquire_lock(connection, str(workspace), "worker")
    assert lease is not None
    assert release_lock(connection, lease)

    connection.close()
    second = connect_memory_db(workspace)
    apply_migrations(second)
    verify_schema(second)
    assert second.execute("SELECT status FROM schema_meta WHERE version=1").fetchone()[0] == "applied"
    second.close()
