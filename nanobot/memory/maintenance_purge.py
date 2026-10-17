"""Independent C-P6 cleanup, explicitly authorized after the P6 data purge."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout

from nanobot.memory.legacy_purge import (
    MARKER_TABLE,
    PurgeError,
    _assert_no_live_leases,
    _completed,
    _connect,
)

PURGE_ID = "legacy-maintenance-cp6-v1"
TABLES = ("maintenance_jobs", "maintenance_lock")


def _plan(connection: sqlite3.Connection) -> dict:
    p6_completed = _completed(connection)
    completed = p6_completed and connection.execute(
        f"SELECT 1 FROM {MARKER_TABLE} WHERE purge_id=?", (PURGE_ID,),
    ).fetchone() is not None
    steps = [{"step": i, "table": table, "operation": "delete", "where": "1",
              "rows": connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]}
             for i, table in enumerate(TABLES, 1)]
    plan = {"purge_id": PURGE_ID, "p6_completed": p6_completed,
            "completed": completed, "steps": steps}
    return {**plan, "plan_digest": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()}


def preview_maintenance_purge(database: str | Path) -> dict:
    """Preview just two tables without migration, marker or filesystem writes."""
    with closing(_connect(database, readonly=True)) as connection:
        connection.execute("BEGIN")
        try:
            return {**_plan(connection), "status": "dry_run", "total_changes": connection.total_changes}
        finally:
            connection.rollback()


def _purge_maintenance(connection: sqlite3.Connection, expected_plan: str) -> dict:
    if connection.in_transaction or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise PurgeError("维护清理必须使用空闲独立连接且 foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = _plan(connection)
        if plan["completed"]:
            connection.commit()
            return {"purge_id": PURGE_ID, "status": "already_completed", "deleted_rows": 0}
        if not plan["p6_completed"]:
            raise PurgeError("必须先完成 P6 数据清理，维护表清理不能绕过前置")
        if plan["plan_digest"] != expected_plan:
            raise PurgeError("维护清单已改变，请停写后重新 dry-run 并核对")
        _assert_no_live_leases(connection)
        # Unlike P6, these two tables have no cyclic FK and keep FK enforcement on.
        for table in TABLES:
            connection.execute(f"DELETE FROM {table}")
        if any(step["rows"] for step in _plan(connection)["steps"]):
            raise PurgeError("维护清理后仍有范围内数据")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise PurgeError("维护清理存在外键违规")
        connection.execute(
            f"INSERT INTO {MARKER_TABLE}(purge_id,completed_at,plan_digest) VALUES(?,?,?)",
            (PURGE_ID, datetime.now(UTC).isoformat(), expected_plan),
        )
        connection.commit()
        return {**plan, "status": "completed"}
    except BaseException:
        connection.rollback()
        raise


def purge_maintenance_data(database: str | Path, *, expected_plan: str, writers_stopped: bool) -> dict:
    """Run only after independent C-P6 approval and stopping all DB users.

    Share P6's executor mutex, but use a separate transaction and fixed marker.
    Never clear memory_outbox, assets, P6 markers or filesystem targets.
    """
    if not writers_stopped or not expected_plan:
        raise PurgeError("维护清理须独立授权、停止所有写入并提供最新预览摘要")
    path = Path(database).expanduser().resolve(strict=True)
    try:
        with FileLock(str(path) + ".p6.lock", timeout=5):
            with closing(_connect(path, readonly=False)) as connection:
                return _purge_maintenance(connection, expected_plan)
    except (sqlite3.OperationalError, Timeout) as exc:
        raise PurgeError("维护清理未取得数据库独占访问或执行失败；请确认所有使用者已停止") from exc
