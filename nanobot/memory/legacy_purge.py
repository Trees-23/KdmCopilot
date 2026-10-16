"""Explicit, offline, once-only P6 cleanup; never called by startup or migrations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PURGE_ID = "self-evolution-p6-v1"
MARKER_TABLE = "data_purge_completions"
SKILL_FILTER = (
    "skill_id LIKE 'skill:m19:%' OR skill_id LIKE 'skill:recovery-%' "
    "OR skill_id LIKE 'skill:evolution-%' OR skill_id='skill-overlay-hot-reload-m9'"
)


class PurgeError(RuntimeError):
    """Cleanup preconditions or postconditions were not satisfied."""


@dataclass(frozen=True)
class PurgeTarget:
    step: int
    table: str
    where: str = "1"
    update: bool = False

    @property
    def statement(self) -> str:
        operation = "UPDATE skills SET current_revision_id=NULL" if self.update else f"DELETE FROM {self.table}"
        return f"{operation} WHERE {self.where}"


# Spec 1 §12.1; the two budget children in step 12 were explicitly approved.
PURGE_TARGETS = (
    PurgeTarget(1, "evolution_candidate_staging"),
    PurgeTarget(2, "evolution_task_evidence"),
    PurgeTarget(3, "evolution_step_evidence"),
    PurgeTarget(4, "evolution_task_links"),
    PurgeTarget(5, "recovery_episodes"),
    PurgeTarget(6, "recovery_case_reviews"),
    PurgeTarget(7, "failure_issue_candidates"),
    PurgeTarget(8, "failure_issue_actions"),
    PurgeTarget(8, "failure_issue_deliveries"),
    PurgeTarget(9, "failure_issues"),
    PurgeTarget(10, "semantic_task_evidence"),
    PurgeTarget(11, "semantic_quality_reviews"),
    PurgeTarget(12, "evaluation_candidate_admissions"),
    PurgeTarget(12, "evaluation_model_call_reservations"),
    PurgeTarget(12, "ab_case_results"),
    PurgeTarget(12, "ab_evaluations"),
    PurgeTarget(13, "eval_case_results"),
    PurgeTarget(13, "eval_runs"),
    PurgeTarget(13, "eval_cases"),
    PurgeTarget(14, "eval_packs"),
    PurgeTarget(15, "proposal_actions"),
    PurgeTarget(15, "proposal_deliveries"),
    PurgeTarget(16, "skill_proposals"),
    PurgeTarget(17, "cases"),
    PurgeTarget(18, "wiki_pages", "page_type='case'"),
    PurgeTarget(19, "skills", f"({SKILL_FILTER}) AND current_revision_id IS NOT NULL", True),
    PurgeTarget(20, "skill_revisions", SKILL_FILTER),
    PurgeTarget(21, "skills", SKILL_FILTER),
    PurgeTarget(22, "retrieval_events"),
    PurgeTarget(23, "memory_assets"),
)


def _connect(database: str | Path, *, readonly: bool) -> sqlite3.Connection:
    # Do not use the normal DB opener: it creates paths and may be followed by migrations.
    path = Path(database).expanduser().resolve(strict=True)
    if not path.is_file():
        raise PurgeError("必须指定已有的 memory.sqlite3 文件")
    connection = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=5)
    connection.execute("PRAGMA foreign_keys=ON")
    if readonly:
        connection.execute("PRAGMA query_only=ON")
    else:
        # In WAL mode this also rejects idle connections holding shared WAL locks.
        # Hold exclusivity until close, including the post-commit verification window.
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
    return connection


def _completed(connection: sqlite3.Connection) -> bool:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (MARKER_TABLE,),
    ).fetchone():
        return False
    return connection.execute(
        f"SELECT 1 FROM {MARKER_TABLE} WHERE purge_id=?", (PURGE_ID,),
    ).fetchone() is not None


def _plan(connection: sqlite3.Connection) -> dict:
    completed = _completed(connection)
    # After Q4 some legacy tables no longer exist. A completed cleanup stays a no-op.
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    steps = []
    for target in PURGE_TARGETS:
        if target.table not in tables and not completed:
            raise PurgeError(f"清理前缺少表：{target.table}；请先部署对应结构，不在清理中执行迁移")
        count = connection.execute(
            f"SELECT COUNT(*) FROM {target.table} WHERE {target.where}",
        ).fetchone()[0] if target.table in tables else None
        steps.append({"step": target.step, "table": target.table, "where": target.where,
                      "operation": "update" if target.update else "delete", "rows": count})
    payload = {"purge_id": PURGE_ID, "completed": completed, "steps": steps}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {**payload, "plan_digest": digest}


def preview_legacy_purge(database: str | Path) -> dict:
    """Read one consistent snapshot; never create a DB, marker or migration."""
    with closing(_connect(database, readonly=True)) as connection:
        connection.execute("BEGIN")
        try:
            return {**_plan(connection), "status": "dry_run", "total_changes": connection.total_changes}
        finally:
            connection.rollback()


def _assert_no_live_leases(connection: sqlite3.Connection) -> None:
    now = datetime.now(UTC)
    for table, column, where in (
        ("maintenance_lock", "lease_until", "1"),
        ("maintenance_jobs", "lease_until", "lease_until IS NOT NULL"),
        ("evolution_candidate_staging", "lease_expires_at", "status='evaluating'"),
        ("ab_evaluations", "execution_deadline", "status='running'"),
    ):
        for (value,) in connection.execute(f"SELECT {column} FROM {table} WHERE {where}"):
            # Legacy running evaluations without a deadline are historical; offline
            # exclusivity and the operator's stopped-writer precondition still apply.
            if value is None:
                continue
            try:
                deadline = datetime.fromisoformat(value)
                if deadline.tzinfo is None:
                    raise ValueError("missing timezone")
            except (ValueError, TypeError) as exc:
                raise PurgeError(f"{table} 存在无效租约，拒绝清理") from exc
            if deadline > now:
                raise PurgeError(f"{table} 仍有有效租约，停止执行者并等待租约释放或到期")


def _purge(connection: sqlite3.Connection, expected_plan: str) -> dict:
    """Use only an idle dedicated connection with foreign keys initially enabled."""
    if connection.in_transaction or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise PurgeError("清理必须使用空闲独立连接且 foreign_keys=ON")
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        # Must recheck inside the write lock, even when preview saw no marker.
        if _completed(connection):
            connection.commit()
            return {"purge_id": PURGE_ID, "status": "already_completed", "deleted_rows": 0}
        plan = _plan(connection)
        if plan["plan_digest"] != expected_plan:
            raise PurgeError("清单已改变，请停写后重新 dry-run 并核对")
        _assert_no_live_leases(connection)
        for target in PURGE_TARGETS:
            connection.execute(target.statement)
        if any(step["rows"] for step in _plan(connection)["steps"]):
            raise PurgeError("清理后仍有范围内数据")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise PurgeError("P6 清理存在外键违规")
        # DDL is transactional too. No migration number and no startup hook.
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} ("
            "purge_id TEXT PRIMARY KEY, completed_at TEXT NOT NULL, plan_digest TEXT NOT NULL)"
        )
        connection.execute(
            f"INSERT INTO {MARKER_TABLE}(purge_id,completed_at,plan_digest) VALUES(?,?,?)",
            (PURGE_ID, datetime.now(UTC).isoformat(), expected_plan),
        )
        connection.commit()
        return {**plan, "status": "completed"}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def purge_legacy_data(database: str | Path, *, expected_plan: str, writers_stopped: bool) -> dict:
    """Execute only after explicit approval and externally stopping ALL writers.

    The flag records the operator precondition; it cannot stop scheduled processes.
    Exclusive SQLite access and live-lease checks additionally reject active users.
    No filesystem targets, backups, maintenance rows or audit data are touched.
    """
    if not writers_stopped or not expected_plan:
        raise PurgeError("必须先停止所有写入、核对 dry-run 并提供清单摘要")
    with closing(_connect(database, readonly=False)) as connection:
        try:
            return _purge(connection, expected_plan)
        except sqlite3.OperationalError as exc:
            raise PurgeError("无法取得数据库独占访问或执行清理；确认所有使用者已停止且结构完整") from exc
