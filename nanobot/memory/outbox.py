"""Idempotent memory index outbox operations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

VALID_OBJECT_TYPES = {"memory", "wiki_page", "case", "skill", "trace_summary"}
VALID_OPERATIONS = {"upsert", "archive", "tombstone", "rebuild"}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def enqueue_outbox(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    object_type: str,
    object_id: str,
    content_hash: str,
    operation: str = "upsert",
    revision_id: str | None = None,
    payload: dict[str, object] | None = None,
    now: datetime | None = None,
) -> str:
    """Insert one idempotent task and supersede older hashes for the same object."""

    if object_type not in VALID_OBJECT_TYPES:
        raise ValueError(f"unsupported outbox object type: {object_type}")
    if operation not in VALID_OPERATIONS:
        raise ValueError(f"unsupported outbox operation: {operation}")
    current = now or _now()
    now_text = _iso(current)
    outbox_id = str(uuid4())
    payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT outbox_id FROM memory_outbox WHERE object_type=? AND object_id=? "
            "AND content_hash=? AND operation=?",
            (object_type, object_id, content_hash, operation),
        ).fetchone()
        if existing:
            connection.commit()
            return existing[0]
        connection.execute(
            "UPDATE memory_outbox SET status='superseded',updated_at=? WHERE object_type=? "
            "AND object_id=? AND operation=? AND content_hash<>? "
            "AND status IN ('pending','leased','retry_wait')",
            (now_text, object_type, object_id, operation, content_hash),
        )
        connection.execute(
            "INSERT INTO memory_outbox(outbox_id,workspace,object_type,object_id,revision_id,content_hash,"
            "operation,payload_json,status,attempt_count,next_attempt_at,lease_until,last_error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                outbox_id,
                workspace,
                object_type,
                object_id,
                revision_id,
                content_hash,
                operation,
                payload_json,
                "pending",
                0,
                now_text,
                None,
                None,
                now_text,
                now_text,
            ),
        )
        connection.commit()
        return outbox_id
    except Exception:
        connection.rollback()
        raise


def claim_outbox(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    worker_id: str,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> sqlite3.Row | None:
    """Lease the oldest due task for a worker."""

    current = now or _now()
    now_text = _iso(current)
    lease_text = _iso(current + timedelta(seconds=lease_seconds))
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT * FROM memory_outbox WHERE workspace=? AND "
            "((status IN ('pending','retry_wait') AND next_attempt_at<=?) OR "
            "(status='leased' AND lease_until<=?)) ORDER BY created_at LIMIT 1",
            (workspace, now_text, now_text),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        connection.execute(
            "UPDATE memory_outbox SET status='leased',lease_until=?,attempt_count=attempt_count+1,"
            "updated_at=? WHERE outbox_id=?",
            (lease_text, now_text, row["outbox_id"]),
        )
        connection.commit()
        return connection.execute(
            "SELECT * FROM memory_outbox WHERE outbox_id=?", (row["outbox_id"],)
        ).fetchone()
    except Exception:
        connection.rollback()
        raise


def mark_outbox_done(connection: sqlite3.Connection, outbox_id: str) -> bool:
    cursor = connection.execute(
        "UPDATE memory_outbox SET status='done',lease_until=NULL,last_error=NULL,updated_at=? "
        "WHERE outbox_id=? AND status='leased'",
        (_iso(_now()), outbox_id),
    )
    connection.commit()
    return cursor.rowcount == 1


def mark_outbox_retry(
    connection: sqlite3.Connection,
    outbox_id: str,
    error: str,
    *,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> str:
    """Move a leased task to retry_wait or dead_letter."""

    current = now or _now()
    row = connection.execute(
        "SELECT attempt_count,status FROM memory_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()
    if row is None or row["status"] != "leased":
        return "unchanged"
    attempts = int(row["attempt_count"])
    status = "dead_letter" if attempts >= max_attempts else "retry_wait"
    next_time = current + timedelta(minutes=2 ** max(0, attempts - 1))
    connection.execute(
        "UPDATE memory_outbox SET status=?,lease_until=NULL,next_attempt_at=?,last_error=?,updated_at=? "
        "WHERE outbox_id=? AND status='leased'",
        (status, _iso(next_time), error[:500], _iso(current), outbox_id),
    )
    connection.commit()
    return status
