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
