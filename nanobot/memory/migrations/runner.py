"""Transactional memory schema migration runner."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from nanobot.memory.schema import (
    APP_BUILD,
    BASE_MIGRATION_PATH,
    MIGRATION_ID,
    MIGRATION_VERSION,
    REQUIRED_TABLES,
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


def _record_failed(connection: sqlite3.Connection, message: str, app_build: str) -> None:
    _bootstrap_schema_meta(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO schema_meta(version,migration_id,app_build,schema_hash,status,applied_at,error_message) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(version) DO UPDATE SET "
            "migration_id=excluded.migration_id,app_build=excluded.app_build,schema_hash=excluded.schema_hash,"
            "status='failed',applied_at=excluded.applied_at,error_message=excluded.error_message",
            (
                MIGRATION_VERSION,
                MIGRATION_ID,
                app_build,
                schema_hash(),
                "failed",
                utc_now(),
                message[:500],
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    app_build: str = APP_BUILD,
    sql: str | None = None,
) -> None:
    """Apply the base migration idempotently and verify its resulting schema."""

    if connection.in_transaction:
        raise MigrationError("migration requires an idle SQLite connection")
    _bootstrap_schema_meta(connection)
    expected_hash = schema_hash() if sql is None else schema_hash(sql)
    current = connection.execute(
        "SELECT version,migration_id,schema_hash,status FROM schema_meta "
        "WHERE version=?",
        (MIGRATION_VERSION,),
    ).fetchone()
    if current and current[3] == "applied":
        if current[1] != MIGRATION_ID or current[2] != expected_hash:
            raise MigrationError("applied memory migration does not match checked-in schema")
        verify_schema(connection, expected_hash)
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO schema_meta(version,migration_id,app_build,schema_hash,status,applied_at,error_message) "
            "VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(version) DO UPDATE SET "
            "migration_id=excluded.migration_id,app_build=excluded.app_build,schema_hash=excluded.schema_hash,"
            "status='running',applied_at=excluded.applied_at,error_message=NULL",
            (MIGRATION_VERSION, MIGRATION_ID, app_build, expected_hash, "running", utc_now()),
        )
        migration_text = sql if sql is not None else migration_sql()
        for statement in migration_text.split(";"):
            if statement.strip():
                connection.execute(statement)
        missing = REQUIRED_TABLES - _table_names(connection)
        if missing:
            raise MigrationError(f"migration missing tables: {', '.join(sorted(missing))}")
        connection.execute(
            "UPDATE schema_meta SET status='applied',app_build=?,schema_hash=?,applied_at=?,error_message=NULL "
            "WHERE version=?",
            (app_build, expected_hash, utc_now(), MIGRATION_VERSION),
        )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        _record_failed(connection, str(exc), app_build)
        raise MigrationError(f"memory migration failed: {exc}") from exc
    verify_schema(connection, expected_hash)


def migration_file_path() -> str:
    """Expose the checked-in migration path for evidence and diagnostics."""

    return str(BASE_MIGRATION_PATH)
