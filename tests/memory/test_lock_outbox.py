"""Maintenance lock and outbox concurrency/recovery tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nanobot.memory.db import connect_memory_db_path
from nanobot.memory.lock import acquire_lock, release_lock, renew_lock
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.outbox import (
    claim_outbox,
    enqueue_outbox,
    mark_outbox_done,
    mark_outbox_retry,
)


def _db():
    connection = connect_memory_db_path(":memory:")
    apply_migrations(connection)
    return connection


def test_lock_competition_expiry_generation_and_stale_renewal(tmp_path: Path) -> None:
    first_connection = connect_memory_db_path(tmp_path / "memory.sqlite3")
    apply_migrations(first_connection)
    second_connection = connect_memory_db_path(tmp_path / "memory.sqlite3")
    base = datetime(2026, 9, 25, tzinfo=UTC)
    first = acquire_lock(first_connection, "workspace", "worker-a", now=base, lease_seconds=60)
    assert first is not None and first.generation == 1
    assert acquire_lock(second_connection, "workspace", "worker-b", now=base + timedelta(seconds=1)) is None
    renewed = renew_lock(first_connection, first, now=base + timedelta(seconds=10), lease_seconds=60)
    assert renewed is not None
    takeover = acquire_lock(second_connection, "workspace", "worker-b", now=base + timedelta(seconds=71))
    assert takeover is not None and takeover.generation == 2
    assert renew_lock(first_connection, first, now=base + timedelta(seconds=72)) is None
    assert release_lock(second_connection, takeover)


def test_outbox_idempotency_superseded_retry_and_dead_letter() -> None:
    connection = _db()
    now = datetime(2026, 9, 25, tzinfo=UTC)
    first = enqueue_outbox(
        connection,
        workspace="workspace",
        object_type="memory",
        object_id="m1",
        content_hash="sha256:one",
        now=now,
    )
    assert enqueue_outbox(
        connection,
        workspace="workspace",
        object_type="memory",
        object_id="m1",
        content_hash="sha256:one",
        now=now,
    ) == first
    second = enqueue_outbox(
        connection,
        workspace="workspace",
        object_type="memory",
        object_id="m1",
        content_hash="sha256:two",
        now=now,
    )
    assert connection.execute("SELECT status FROM memory_outbox WHERE outbox_id=?", (first,)).fetchone()[0] == "superseded"
    claimed = claim_outbox(connection, workspace="workspace", worker_id="worker", now=now)
    assert claimed["outbox_id"] == second
    assert mark_outbox_retry(connection, second, "temporary", now=now, max_attempts=2) == "retry_wait"
    claimed = claim_outbox(
        connection,
        workspace="workspace",
        worker_id="worker",
        now=now + timedelta(minutes=2),
    )
    assert claimed["outbox_id"] == second
    assert mark_outbox_retry(connection, second, "permanent", now=now, max_attempts=2) == "dead_letter"


def test_outbox_done_is_terminal() -> None:
    connection = _db()
    outbox_id = enqueue_outbox(
        connection,
        workspace="workspace",
        object_type="trace_summary",
        object_id="t1",
        content_hash="sha256:t",
    )
    assert claim_outbox(connection, workspace="workspace", worker_id="worker")
    assert mark_outbox_done(connection, outbox_id)
    assert not mark_outbox_done(connection, outbox_id)
