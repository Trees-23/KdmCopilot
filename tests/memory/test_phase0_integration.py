"""End-to-end Phase 0 persistence bootstrap in an isolated workspace."""

from __future__ import annotations

from pathlib import Path

from nanobot.memory.db import connect_memory_db, foreign_keys_enabled, resolve_memory_db_path
from nanobot.memory.index import initialize_fts, rebuild_fts
from nanobot.memory.lock import acquire_lock, release_lock
from nanobot.memory.migrations.runner import apply_migrations, verify_schema
from nanobot.memory.outbox import claim_outbox, enqueue_outbox, mark_outbox_done


def test_phase0_bootstrap_is_repeatable_and_isolated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database_path = resolve_memory_db_path(workspace)
    connection = connect_memory_db(workspace)
    apply_migrations(connection)
    verify_schema(connection)
    assert database_path.exists()
    assert foreign_keys_enabled(connection)
    assert initialize_fts(connection).available
    assert rebuild_fts(connection) == 0

    lease = acquire_lock(connection, str(workspace), "worker")
    assert lease is not None
    outbox_id = enqueue_outbox(
        connection,
        workspace=str(workspace),
        object_type="trace_summary",
        object_id="trace-1",
        content_hash="sha256:trace",
    )
    claimed = claim_outbox(connection, workspace=str(workspace), worker_id="worker")
    assert claimed["outbox_id"] == outbox_id
    assert mark_outbox_done(connection, outbox_id)
    assert release_lock(connection, lease)

    connection.close()
    second = connect_memory_db(workspace)
    apply_migrations(second)
    verify_schema(second)
    assert second.execute("SELECT status FROM schema_meta WHERE version=1").fetchone()[0] == "applied"
    second.close()
