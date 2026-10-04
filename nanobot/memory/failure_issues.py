"""Failure-driven evolution Issue cards and bounded human feedback.

This module turns redacted M13 recovery reviews into reviewable Issue cards.
It never copies raw transcripts and never changes an active Skill.  A human
action only records an explicit routing decision; any future repair candidate
must still pass the normal M15 evaluation and publication gates.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Iterable
from uuid import uuid4

from nanobot.memory.derivation import digest
from nanobot.memory.outbox import enqueue_outbox

ISSUE_ACTIONS = {"note", "record_case", "record_memory", "request_candidate", "reject"}
MUTATING_ACTIONS = {"note", "record_case", "record_memory", "request_candidate", "reject"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stable_issue_id(recovery_key: str, episode_ids: Iterable[str]) -> str:
    digest = hashlib.sha256(
        (str(recovery_key) + "|" + "|".join(sorted(str(item) for item in episode_ids))).encode()
    ).hexdigest()[:16]
    return f"recovery-issue-{digest}"


def _load_review_rows(connection: sqlite3.Connection, workspace: str) -> list[tuple[Any, ...]]:
    return connection.execute(
        """SELECT recovery_key,episode_ids_json,status,reason_code,reason_text,created_at
           FROM recovery_case_reviews WHERE workspace=? ORDER BY created_at, recovery_key""",
        (workspace,),
    ).fetchall()


def create_failure_issues(
    connection: sqlite3.Connection,
    *,
    workspace: str,
) -> tuple[str, ...]:
    """Materialize new redacted recovery reviews as idempotent Issue cards."""

    created: list[str] = []
    now = _now()
    for recovery_key, episode_json, review_status, reason_code, reason_text, _created_at in _load_review_rows(
        connection, workspace
    ):
        try:
            episode_ids = tuple(str(item) for item in json.loads(episode_json or "[]") if str(item))
        except (TypeError, ValueError, json.JSONDecodeError):
            episode_ids = ()
        if not episode_ids:
            continue
        issue_id = _stable_issue_id(str(recovery_key), episode_ids)
        sample = connection.execute(
            """SELECT failure_goal,failure_class,correction_goal FROM recovery_episodes
               WHERE workspace=? AND episode_id=?""",
            (workspace, episode_ids[0]),
        ).fetchone()
        goal = str(sample[0]) if sample and sample[0] else "未记录任务目标"
        failure_class = str(sample[1]) if sample and sample[1] else "未分类失败"
        correction = str(sample[2]) if sample and sample[2] else "未记录人工纠正"
        passed = str(review_status) == "passed"
        status = "pending_review" if passed else "insufficient_evidence"
        recommendation = "可选择记录 Case、写入语义记忆或申请修复候选" if passed else "证据不足，仅建议保留失败记录"
        summary = (
            f"任务：{goal}；失败类别：{failure_class}；"
            f"纠正方向：{correction}；质量结论：{str(reason_text)[:240]}"
        )
        title = f"失败恢复改进：{goal[:80]}"
        cursor = connection.execute(
            """INSERT OR IGNORE INTO failure_issues
               (issue_id,workspace,recovery_key,episode_ids_json,title,task_goal,failure_class,
                correction_goal,summary,status,recommendation,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                issue_id, workspace, str(recovery_key), json.dumps(episode_ids, ensure_ascii=False),
                title, goal, failure_class, correction, summary, status, recommendation, now, now,
            ),
        )
        if cursor.rowcount:
            created.append(issue_id)
    connection.commit()
    return tuple(created)


def list_failure_issues(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    statuses: Iterable[str] | None = None,
    limit: int = 20,
) -> tuple[dict[str, Any], ...]:
    if limit < 1 or limit > 100:
        raise ValueError("failure issue list limit must be between 1 and 100")
    values = tuple(str(item) for item in (statuses or ()) if str(item))
    sql = "SELECT issue_id,title,task_goal,failure_class,correction_goal,summary,status,recommendation,created_at,updated_at,review_reason FROM failure_issues WHERE workspace=?"
    params: list[Any] = [workspace]
    if values:
        sql += " AND status IN (" + ",".join("?" for _ in values) + ")"
        params.extend(values)
    sql += " ORDER BY created_at DESC,issue_id DESC LIMIT ?"
    params.append(limit)
    rows = connection.execute(sql, params).fetchall()
    return tuple(
        {
            "issue_id": str(row[0]), "title": str(row[1]), "task_goal": str(row[2] or ""),
            "failure_class": str(row[3]), "correction_goal": str(row[4] or ""),
            "summary": str(row[5]), "status": str(row[6]), "recommendation": str(row[7]),
            "created_at": str(row[8]), "updated_at": str(row[9]), "review_reason": str(row[10] or ""),
        }
        for row in rows
    )


def get_failure_issue(connection: sqlite3.Connection, *, workspace: str, issue_id: str) -> dict[str, Any] | None:
    rows = list_failure_issues(connection, workspace=workspace, limit=100)
    return next((item for item in rows if item["issue_id"] == issue_id), None)


def record_failure_issue_memory(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    issue: dict[str, Any],
    actor: str,
    note: str = "",
) -> str:
    """Persist one redacted recovery rule as a versioned semantic memory.

    Issue fields are already derived by the recovery redaction boundary.  The
    memory body is still bounded and contains no transcript or tool payload.
    Replaying the same issue is idempotent by ``memory_id`` and content hash.
    """

    issue_id = str(issue.get("issue_id") or "").strip()
    if not issue_id or not workspace or not actor:
        raise ValueError("issue, workspace and actor are required")
    memory_id = f"recovery-memory:{hashlib.sha256(issue_id.encode()).hexdigest()[:24]}"
    title = f"失败恢复规则：{str(issue.get('task_goal') or '未命名任务')[:120]}"
    summary = (
        f"失败类别：{str(issue.get('failure_class') or '未分类')[:120]}；"
        f"纠正方向：{str(issue.get('correction_goal') or '未记录')[:240]}"
    )
    content = (
        "适用场景：\n"
        f"- 任务：{str(issue.get('task_goal') or '未记录')[:360]}\n"
        f"- 失败类别：{str(issue.get('failure_class') or '未分类')[:120]}\n\n"
        "恢复规则：\n"
        f"- {str(issue.get('correction_goal') or '按人工纠正方向处理')[:360]}\n"
        "- 仅适用于同类任务；范围不一致时转人工确认。\n"
        "- 不写入凭据、成员身份、完整对话、隐藏推理或完整工具参数。\n"
    )
    now = _now()
    content_hash = digest(content)
    revision_id = f"recovery-memory-revision:{content_hash[7:31]}"
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT current_revision_id FROM memory_records WHERE memory_id=?", (memory_id,)
        ).fetchone()
        if existing and existing[0]:
            current = connection.execute(
                "SELECT content_hash FROM memory_revisions WHERE revision_id=?", (existing[0],)
            ).fetchone()
            if current and str(current[0]) == content_hash:
                connection.commit()
                return memory_id
        connection.execute(
            """INSERT INTO memory_records
               (memory_id,namespace,memory_type,source_actor,title,summary,source_refs_json,
                tags_json,sensitivity,confidence,authority,salience,effective_score,status,
                deletion_state,current_revision_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'[\"failure-recovery\"]','private',0.8,0.8,0.7,0.8,
                      'active','none',?,?,?)
               ON CONFLICT(memory_id) DO UPDATE SET title=excluded.title,summary=excluded.summary,
                 source_refs_json=excluded.source_refs_json,status='active',updated_at=excluded.updated_at""",
            (memory_id, "workspace", "decision", actor, title, summary,
             json.dumps([issue_id], ensure_ascii=False), existing[0] if existing else revision_id,
             now, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO memory_revisions
               (revision_id,memory_id,revision_no,content,summary,content_hash,previous_revision_id,
                author_actor,reason,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (revision_id, memory_id, 1, content, summary, content_hash,
             existing[0] if existing else None, actor, note[:500] or "管理员确认失败恢复规则", now),
        )
        connection.execute(
            "UPDATE memory_records SET current_revision_id=?,updated_at=? WHERE memory_id=?",
            (revision_id, now, memory_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    enqueue_outbox(
        connection,
        workspace=workspace,
        object_type="memory",
        object_id=memory_id,
        revision_id=revision_id,
        content_hash=content_hash,
        operation="upsert",
        payload={"memory_id": memory_id, "revision_id": revision_id},
    )
    return memory_id


def apply_failure_issue_action(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    issue_id: str,
    action: str,
    actor_openid: str,
    group_openid: str,
    note: str = "",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Apply one administrator routing action with durable idempotency."""

    if action not in ISSUE_ACTIONS:
        raise ValueError(f"unsupported failure issue action: {action}")
    if not actor_openid or not group_openid:
        raise ValueError("actor and group are required")
    issue = get_failure_issue(connection, workspace=workspace, issue_id=issue_id)
    if issue is None:
        raise LookupError("failure issue not found")
    key = idempotency_key or f"failure-issue:{issue_id}:{action}:{actor_openid}:{hashlib.sha256(note.encode()).hexdigest()[:12]}"
    existing = connection.execute(
        "SELECT result_status FROM failure_issue_actions WHERE idempotency_key=?", (key,)
    ).fetchone()
    if existing:
        return {"issue_id": issue_id, "status": str(existing[0]), "idempotent": True}
    current = issue["status"]
    if action == "note":
        next_status = current
    elif current != "pending_review":
        raise ValueError(f"issue status does not accept action: {current}")
    elif action == "record_case":
        next_status = "recorded_case"
    elif action == "record_memory":
        next_status = "memory_recorded"
    elif action == "request_candidate":
        if issue["recommendation"].startswith("证据不足"):
            raise ValueError("recovery evidence is insufficient for a repair candidate")
        next_status = "candidate_requested"
    else:
        next_status = "rejected"
    now = _now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """INSERT INTO failure_issue_actions
               (action_id,issue_id,workspace,actor_openid,group_openid,action,note,result_status,idempotency_key,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (f"failure-action:{uuid4()}", issue_id, workspace, actor_openid, group_openid,
             action, note[:1000], next_status, key, now),
        )
        connection.execute(
            """UPDATE failure_issues SET status=?,updated_at=?,reviewed_at=?,reviewed_by=?,review_reason=?
               WHERE issue_id=? AND workspace=?""",
            (next_status, now, now if action != "note" else issue["updated_at"], actor_openid,
             note[:1000] if note else issue["review_reason"], issue_id, workspace),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if action == "record_memory":
        record_failure_issue_memory(
            connection,
            workspace=workspace,
            issue=issue,
            actor=actor_openid,
            note=note,
        )
    return {"issue_id": issue_id, "status": next_status, "idempotent": False}


__all__ = [
    "ISSUE_ACTIONS", "MUTATING_ACTIONS", "apply_failure_issue_action", "create_failure_issues",
    "get_failure_issue", "list_failure_issues", "record_failure_issue_memory",
]
