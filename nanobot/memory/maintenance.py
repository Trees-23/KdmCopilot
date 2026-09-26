"""Durable maintenance scheduling and cursor operations.

Maintenance state is derived coordination metadata.  It never changes Wiki,
Skill, Audit, or Markdown facts and is safe to abandon when a worker dies.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from nanobot.memory.db import connect_memory_db
from nanobot.memory.lock import acquire_lock, release_lock
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.outbox import claim_outbox, mark_outbox_done, mark_outbox_retry

IDLE_SECONDS = 45 * 60
MAX_RETRIES = 5


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _iso(value: datetime | None = None) -> str:
    return _now(value).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class MaintenanceJob:
    job_id: str
    workspace: str
    session_key: str
    activity_epoch: int
    last_activity_at: str
    last_message_cursor: str
    last_review_cursor: str
    snapshot_cursor: str
    due_at: str
    status: str
    retry_count: int
    worker_id: str | None


def open_maintenance_db(workspace: str | Path) -> sqlite3.Connection:
    connection = connect_memory_db(workspace)
    apply_migrations(connection)
    return connection


def upsert_activity(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    session_key: str,
    message_cursor: str,
    now: datetime | None = None,
) -> MaintenanceJob:
    """Coalesce activity into one job per workspace/session with a 45-minute debounce."""

    if not workspace or not session_key or not message_cursor:
        raise ValueError("workspace, session_key and message_cursor are required")
    current = _now(now)
    timestamp = _iso(current)
    due = _iso(current + timedelta(seconds=IDLE_SECONDS))
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT job_id,activity_epoch,last_review_cursor,snapshot_cursor,retry_count "
            "FROM maintenance_jobs WHERE workspace=? AND session_key=?",
            (workspace, session_key),
        ).fetchone()
        if existing is None:
            job_id = str(uuid4())
            epoch = 1
            review_cursor = ""
            snapshot_cursor = ""
            retries = 0
            connection.execute(
                """INSERT INTO maintenance_jobs
                (job_id,workspace,session_key,activity_epoch,last_activity_at,last_message_cursor,
                 last_review_cursor,snapshot_cursor,due_at,lease_until,status,retry_count,error_message,
                 worker_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,NULL,'scheduled',?,NULL,NULL,?,?)""",
                (job_id, workspace, session_key, epoch, timestamp, message_cursor, review_cursor,
                 snapshot_cursor, due, retries, timestamp, timestamp),
            )
        else:
            job_id = str(existing[0])
            epoch = int(existing[1]) + 1
            review_cursor = str(existing[2] or "")
            snapshot_cursor = str(existing[3] or "")
            retries = int(existing[4] or 0)
            connection.execute(
                """UPDATE maintenance_jobs SET activity_epoch=?,last_activity_at=?,last_message_cursor=?,
                   due_at=?,status='scheduled',retry_count=?,error_message=NULL,worker_id=NULL,updated_at=?
                   WHERE job_id=?""",
                (epoch, timestamp, message_cursor, due, retries, timestamp, job_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return get_job(connection, job_id)  # type: ignore[return-value]


def get_job(connection: sqlite3.Connection, job_id: str) -> MaintenanceJob | None:
    row = connection.execute("SELECT * FROM maintenance_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    return MaintenanceJob(
        job_id=row["job_id"], workspace=row["workspace"], session_key=row["session_key"],
        activity_epoch=row["activity_epoch"], last_activity_at=row["last_activity_at"],
        last_message_cursor=row["last_message_cursor"], last_review_cursor=row["last_review_cursor"],
        snapshot_cursor=row["snapshot_cursor"], due_at=row["due_at"], status=row["status"],
        retry_count=row["retry_count"], worker_id=row["worker_id"],
    )


def claim_due_job(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    worker_id: str,
    now: datetime | None = None,
) -> MaintenanceJob | None:
    """Claim one idle job, recovering a stale running lease after restart."""

    current = _now(now)
    timestamp = _iso(current)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """SELECT job_id FROM maintenance_jobs WHERE workspace=?
               AND due_at<=? AND (status IN ('scheduled','retry_wait')
                 OR (status='running' AND lease_until<=?))
               ORDER BY due_at LIMIT 1""",
            (workspace, timestamp, timestamp),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        cursor = connection.execute(
            """UPDATE maintenance_jobs SET status='running',worker_id=?,lease_until=?,updated_at=?
               WHERE job_id=? AND (status IN ('scheduled','retry_wait')
                 OR (status='running' AND lease_until<=?))""",
            (worker_id, _iso(current + timedelta(seconds=60)), timestamp, row[0], timestamp),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return get_job(connection, str(row[0]))


def complete_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    worker_id: str,
    epoch: int,
    review_cursor: str,
    snapshot_cursor: str,
    now: datetime | None = None,
) -> bool:
    """Complete only the current worker/epoch; stale workers cannot overwrite cursors."""

    timestamp = _iso(now)
    cursor = connection.execute(
        """UPDATE maintenance_jobs SET status='done',last_review_cursor=?,snapshot_cursor=?,
           lease_until=NULL,error_message=NULL,updated_at=?
           WHERE job_id=? AND worker_id=? AND activity_epoch=? AND status='running'""",
        (review_cursor, snapshot_cursor, timestamp, job_id, worker_id, epoch),
    )
    connection.commit()
    return cursor.rowcount == 1


def fail_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    worker_id: str,
    error: str,
    max_retries: int = MAX_RETRIES,
    now: datetime | None = None,
) -> str:
    """Move a failed review to retry_wait or dead_letter with bounded backoff."""

    current = _now(now)
    row = connection.execute(
        "SELECT retry_count,status FROM maintenance_jobs WHERE job_id=? AND worker_id=?",
        (job_id, worker_id),
    ).fetchone()
    if row is None or row[1] != "running":
        return "unchanged"
    attempts = int(row[0]) + 1
    status = "dead_letter" if attempts >= max_retries else "retry_wait"
    due = current + timedelta(minutes=2 ** max(0, attempts - 1))
    connection.execute(
        """UPDATE maintenance_jobs SET status=?,retry_count=?,error_message=?,lease_until=NULL,
           due_at=?,updated_at=? WHERE job_id=? AND worker_id=? AND status='running'""",
        (status, attempts, error[:500], _iso(due), _iso(current), job_id, worker_id),
    )
    connection.commit()
    return status


def process_one_outbox(connection: sqlite3.Connection, *, workspace: str, worker_id: str) -> str:
    """Drain one derived-index outbox item; provider facts are never changed here."""

    row = claim_outbox(connection, workspace=workspace, worker_id=worker_id)
    if row is None:
        return "empty"
    try:
        # Actual provider synchronization is supplied by a future adapter-aware
        # worker.  Marking the derived task done is safe only for metadata-only
        # rebuilds; callers pass a processor for provider work instead.
        operation = str(row["operation"])
        if operation == "rebuild":
            mark_outbox_done(connection, row["outbox_id"])
            return "done"
        mark_outbox_retry(connection, row["outbox_id"], "provider processor unavailable")
        return "retry_wait"
    except Exception as exc:
        mark_outbox_retry(connection, row["outbox_id"], str(exc))
        return "retry_wait"


__all__ = [
    "IDLE_SECONDS", "MAX_RETRIES", "MaintenanceJob", "acquire_lock", "claim_due_job",
    "complete_job", "fail_job", "get_job", "open_maintenance_db", "process_one_outbox",
    "release_lock", "upsert_activity",
]
