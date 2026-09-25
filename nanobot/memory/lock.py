"""Workspace maintenance lease operations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class LockLease:
    workspace: str
    owner: str
    generation: int
    lease_until: str


def acquire_lock(
    connection: sqlite3.Connection,
    workspace: str,
    owner: str,
    *,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> LockLease | None:
    """Acquire or take over an expired lock, incrementing its generation."""

    if not workspace or not owner:
        raise ValueError("workspace and owner are required")
    current_time = now or _now()
    now_text = _iso(current_time)
    lease_text = _iso(current_time + timedelta(seconds=lease_seconds))
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT owner,lease_until,generation FROM maintenance_lock WHERE workspace=?",
            (workspace,),
        ).fetchone()
        if row is not None and row[0] != owner and _parse_time(row[1]) > current_time:
            connection.rollback()
            return None
        if row is None:
            generation = 1
            connection.execute(
                "INSERT INTO maintenance_lock(workspace,owner,lease_until,acquired_at,renewed_at,"
                "generation,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (workspace, owner, lease_text, now_text, now_text, generation, now_text, now_text),
            )
        else:
            generation = int(row[2]) + 1
            connection.execute(
                "UPDATE maintenance_lock SET owner=?,lease_until=?,acquired_at=?,renewed_at=?,"
                "generation=?,updated_at=? WHERE workspace=?",
                (owner, lease_text, now_text, now_text, generation, now_text, workspace),
            )
        connection.commit()
        return LockLease(workspace, owner, generation, lease_text)
    except Exception:
        connection.rollback()
        raise


def renew_lock(
    connection: sqlite3.Connection,
    lease: LockLease,
    *,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> LockLease | None:
    """Renew only the exact owner and generation; stale workers cannot renew."""

    current_time = now or _now()
    now_text = _iso(current_time)
    lease_text = _iso(current_time + timedelta(seconds=lease_seconds))
    cursor = connection.execute(
        "UPDATE maintenance_lock SET lease_until=?,renewed_at=?,updated_at=? "
        "WHERE workspace=? AND owner=? AND generation=?",
        (lease_text, now_text, now_text, lease.workspace, lease.owner, lease.generation),
    )
    connection.commit()
    if cursor.rowcount != 1:
        return None
    return LockLease(lease.workspace, lease.owner, lease.generation, lease_text)


def release_lock(connection: sqlite3.Connection, lease: LockLease) -> bool:
    """Release only the exact owner and generation."""

    cursor = connection.execute(
        "DELETE FROM maintenance_lock WHERE workspace=? AND owner=? AND generation=?",
        (lease.workspace, lease.owner, lease.generation),
    )
    connection.commit()
    return cursor.rowcount == 1
