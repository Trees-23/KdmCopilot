"""Transactional memory schema migration runner."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from nanobot.memory.schema import (
    APP_BUILD,
    BASE_REQUIRED_TABLES,
    MIGRATION_ID,
    MIGRATION_VERSION,
    PHASE6_MIGRATION_PATH,
    REQUIRED_TABLES,
    base_migration_sql,
    migration_sql,
    schema_hash,
)


class MigrationError(RuntimeError):
    """Raised when a memory migration cannot be applied or verified."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _bootstrap_schema_meta(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
          version INTEGER PRIMARY KEY,
          migration_id TEXT NOT NULL UNIQUE,
          app_build TEXT NOT NULL,
          schema_hash TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('running','applied','failed','rolled_back')),
          applied_at TEXT NOT NULL,
          error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS schema_meta_status ON schema_meta(status,version DESC);
        """
    )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in rows}


def verify_schema(connection: sqlite3.Connection, expected_hash: str | None = None) -> None:
    """Verify the applied schema has all required tables and matching metadata."""

    missing = REQUIRED_TABLES - _table_names(connection)
    if missing:
        raise MigrationError(f"memory schema missing tables: {', '.join(sorted(missing))}")
    row = connection.execute(
        "SELECT version,migration_id,schema_hash,status FROM schema_meta "
        "WHERE version=?",
        (MIGRATION_VERSION,),
    ).fetchone()
    if row is None or row[1] != MIGRATION_ID or row[3] != "applied":
        raise MigrationError("memory schema metadata is not applied")
    if expected_hash is not None and row[2] != expected_hash:
        raise MigrationError("memory schema hash mismatch")


def _verify_version(
    connection: sqlite3.Connection,
    *,
    version: int,
    migration_id: str,
    expected_hash: str,
    required_tables: set[str] | frozenset[str],
) -> None:
    missing = required_tables - _table_names(connection)
    if missing:
        raise MigrationError(f"memory schema missing tables: {', '.join(sorted(missing))}")
    row = connection.execute(
        "SELECT migration_id,schema_hash,status FROM schema_meta WHERE version=?", (version,)
    ).fetchone()
    if row is None or row[0] != migration_id or row[2] != "applied":
        raise MigrationError(f"memory migration {version} is not applied")
    if row[1] != expected_hash:
        raise MigrationError(f"memory migration {version} schema hash mismatch")


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    app_build: str = APP_BUILD,
    sql: str | None = None,
) -> None:
    """Apply all checked-in migrations idempotently and verify the schema.

    ``sql`` remains a test-only escape hatch for exercising failed migration
    recovery. It is applied as the base migration and never replaces the
    checked-in Phase 6 migration.
    """

    if connection.in_transaction:
        raise MigrationError("migration requires an idle SQLite connection")
    _bootstrap_schema_meta(connection)

    def apply_one(version: int, migration_id: str, migration_text: str,
                  required_tables: set[str] | frozenset[str]) -> None:
        expected_hash = schema_hash(migration_text)
        current = connection.execute(
            "SELECT migration_id,schema_hash,status FROM schema_meta WHERE version=?", (version,)
        ).fetchone()
        if current and current[2] == "applied":
            if current[0] != migration_id or current[1] != expected_hash:
                raise MigrationError(f"applied memory migration {version} does not match checked-in schema")
            _verify_version(connection, version=version, migration_id=migration_id,
                            expected_hash=expected_hash, required_tables=required_tables)
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "INSERT INTO schema_meta(version,migration_id,app_build,schema_hash,status,applied_at,error_message) "
                "VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(version) DO UPDATE SET "
                "migration_id=excluded.migration_id,app_build=excluded.app_build,schema_hash=excluded.schema_hash,"
                "status='running',applied_at=excluded.applied_at,error_message=NULL",
                (version, migration_id, app_build, expected_hash, "running", utc_now()),
            )
            for statement in migration_text.split(";"):
                if statement.strip():
                    connection.execute(statement)
            missing = required_tables - _table_names(connection)
            if missing:
                raise MigrationError(f"migration missing tables: {', '.join(sorted(missing))}")
            connection.execute(
                "UPDATE schema_meta SET status='applied',app_build=?,schema_hash=?,applied_at=?,error_message=NULL "
                "WHERE version=?",
                (app_build, expected_hash, utc_now(), version),
            )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            _record_failed_version(connection, version, migration_id, expected_hash, str(exc), app_build)
            raise MigrationError(f"memory migration failed: {exc}") from exc
        _verify_version(connection, version=version, migration_id=migration_id,
                        expected_hash=expected_hash, required_tables=required_tables)

    if sql is not None:
        apply_one(1, "0001_memory_base", sql, BASE_REQUIRED_TABLES)
        return
    apply_one(1, "0001_memory_base", base_migration_sql(), BASE_REQUIRED_TABLES)
    apply_one(MIGRATION_VERSION, MIGRATION_ID, migration_sql(), REQUIRED_TABLES)
    verify_schema(connection, schema_hash(migration_sql()))


def _record_failed_version(
    connection: sqlite3.Connection,
    version: int,
    migration_id: str,
    expected_hash: str,
    message: str,
    app_build: str,
) -> None:
    """Record a failed migration without assuming it is the latest version."""
    _bootstrap_schema_meta(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO schema_meta(version,migration_id,app_build,schema_hash,status,applied_at,error_message) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(version) DO UPDATE SET "
            "migration_id=excluded.migration_id,app_build=excluded.app_build,schema_hash=excluded.schema_hash,"
            "status='failed',applied_at=excluded.applied_at,error_message=excluded.error_message",
            (version, migration_id, app_build, expected_hash, "failed", utc_now(), message[:500]),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def migration_file_path() -> str:
    """Expose the latest checked-in migration path for evidence and diagnostics."""

    return str(PHASE6_MIGRATION_PATH)
