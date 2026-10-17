"""Durable Gateway triggers and independent bounded worker execution leases."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

MAX_ATTEMPTS = 3
LEASE_SECONDS = 120


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class WorkLease:
    work_id: str
    workspace: str
    kind: str
    owner: str
    generation: int


def enqueue_work(connection: sqlite3.Connection, workspace: str, kind: str, trigger_id: str,
                 *, now: datetime | None = None) -> str:
    """Duplicate callbacks reuse the same durable trigger, including terminal rows."""
    if kind not in {"review", "lesson"} or not workspace or not trigger_id:
        raise ValueError("invalid evolution work identity")
    timestamp = _stamp(_now(now))
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT work_id FROM evolution_work_items WHERE workspace=? AND kind=? "
            "AND trigger_id=? LIMIT 1",
            (workspace, kind, trigger_id),
        ).fetchone()
        if row:
            connection.commit()
            return str(row[0])
        work_id = "evolution-work:" + uuid4().hex
        connection.execute(
            "INSERT INTO evolution_work_items(work_id,workspace,kind,trigger_id,status,available_at,created_at,updated_at) "
            "VALUES(?,?,?,?,'queued',?,?,?)", (work_id, workspace, kind, trigger_id, timestamp, timestamp, timestamp),
        )
        connection.commit()
        return work_id
    except BaseException:
        connection.rollback()
        raise


def reap_work(connection: sqlite3.Connection, workspace: str, *, now: datetime | None = None) -> int:
    timestamp = _stamp(_now(now))
    cursor = connection.execute(
        "UPDATE evolution_work_items SET status=CASE WHEN attempt_count>=? THEN 'failed' ELSE 'queued' END, "
        "lease_owner=NULL,lease_expires_at=NULL,error_code='worker_lease_expired',updated_at=?,available_at=? "
        "WHERE workspace=? AND status='running' AND lease_expires_at<=?",
        (MAX_ATTEMPTS, timestamp, timestamp, workspace, timestamp),
    )
    connection.commit()
    return cursor.rowcount


def claim_work(connection: sqlite3.Connection, workspace: str, owner: str,
               *, now: datetime | None = None) -> WorkLease | None:
    current = _now(now)
    if not owner:
        raise ValueError("worker owner is required")
    reap_work(connection, workspace, now=current)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT work_id,kind,generation FROM evolution_work_items WHERE workspace=? AND status='queued' "
            "AND available_at<=? ORDER BY created_at,work_id LIMIT 1", (workspace, _stamp(current)),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        connection.execute(
            "UPDATE evolution_work_items SET status='running',attempt_count=attempt_count+1,"
            "generation=generation+1,lease_owner=?,lease_expires_at=?,updated_at=? WHERE work_id=?",
            (owner, _stamp(current + timedelta(seconds=LEASE_SECONDS)), _stamp(current), row[0]),
        )
        connection.commit()
        return WorkLease(str(row[0]), workspace, str(row[1]), owner, int(row[2]) + 1)
    except BaseException:
        connection.rollback()
        raise


def renew_work(connection: sqlite3.Connection, lease: WorkLease, *, now: datetime | None = None) -> bool:
    current = _now(now)
    updated = connection.execute(
        "UPDATE evolution_work_items SET lease_expires_at=?,updated_at=? WHERE work_id=? AND workspace=? "
        "AND status='running' AND lease_owner=? AND generation=? AND lease_expires_at>?",
        (_stamp(current + timedelta(seconds=LEASE_SECONDS)), _stamp(current), lease.work_id, lease.workspace,
         lease.owner, lease.generation, _stamp(current)),
    )
    connection.commit()
    return updated.rowcount == 1


def finish_work(connection: sqlite3.Connection, lease: WorkLease, *, error_code: str | None = None,
                result: dict | None = None, now: datetime | None = None) -> bool:
    current = _now(now)
    updated = connection.execute(
        "UPDATE evolution_work_items SET status=CASE WHEN ? IS NULL THEN 'completed' "
        "WHEN attempt_count>=? THEN 'failed' ELSE 'queued' END, lease_owner=NULL,lease_expires_at=NULL, "
        "available_at=?,error_code=?,result_json=?,updated_at=? WHERE work_id=? AND workspace=? "
        "AND status='running' AND lease_owner=? AND generation=? AND lease_expires_at>?",
        (error_code, MAX_ATTEMPTS, _stamp(current + timedelta(seconds=60)), error_code,
         json.dumps(result or {}, sort_keys=True), _stamp(current), lease.work_id, lease.workspace,
         lease.owner, lease.generation, _stamp(current)),
    )
    connection.commit()
    return updated.rowcount == 1
