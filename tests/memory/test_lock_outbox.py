"""Maintenance lock and outbox concurrency/recovery tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nanobot.memory.db import connect_memory_db_path
from nanobot.memory.lock import acquire_lock, release_lock
from nanobot.memory.migrations.runner import apply_migrations


def test_lock_competition_expiry_generation_and_stale_release(tmp_path: Path) -> None:
    first_connection = connect_memory_db_path(tmp_path / "memory.sqlite3")
    apply_migrations(first_connection)
    second_connection = connect_memory_db_path(tmp_path / "memory.sqlite3")
    base = datetime(2026, 9, 25, tzinfo=UTC)
    first = acquire_lock(first_connection, "workspace", "worker-a", now=base, lease_seconds=60)
    assert first is not None and first.generation == 1
    assert acquire_lock(second_connection, "workspace", "worker-b", now=base + timedelta(seconds=1)) is None
    takeover = acquire_lock(second_connection, "workspace", "worker-b", now=base + timedelta(seconds=71))
    assert takeover is not None and takeover.generation == 2
    assert not release_lock(first_connection, first)
    assert release_lock(second_connection, takeover)
