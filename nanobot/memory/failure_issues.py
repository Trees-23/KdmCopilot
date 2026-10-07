"""Failure-driven evolution Issue cards and bounded human feedback.

This module turns redacted M13 recovery reviews into reviewable Issue cards.
It never copies raw transcripts and never changes an active Skill.  A human
action only records an explicit routing decision; any future repair candidate
must still pass the normal M15 evaluation and publication gates.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, Iterable
from uuid import uuid4

from nanobot.audit.redaction import AuditRedactor, RedactionError
from nanobot.memory.derivation import digest
from nanobot.memory.outbox import enqueue_outbox

ISSUE_ACTIONS = {"note", "record_case", "record_memory", "request_candidate", "revise", "reject"}
MUTATING_ACTIONS = set(ISSUE_ACTIONS)
_DIRECTION_SPACE_RE = re.compile(r"\s+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|token|password|secret|credential|authorization)\s*[:=]\s*[^\s,;]+"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]*")
_COMMAND_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:sudo\s+|rm\s+-|curl\s+|wget\s+|bash\s+|sh\s+|powershell\s+|python(?:3)?\s+|chmod\s+|git\s+)[^;\n]{0,240}"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stable_issue_id(recovery_key: str, episode_ids: Iterable[str]) -> str:
    digest = hashlib.sha256(
        (str(recovery_key) + "|" + "|".join(sorted(str(item) for item in episode_ids))).encode()
    ).hexdigest()[:16]
    return f"recovery-issue-{digest}"


def normalize_revise_direction(value: str) -> str:
    """Normalize an administrator direction before hashing it for replay safety."""

    return _DIRECTION_SPACE_RE.sub(" ", str(value or "").strip()).casefold()


def revise_direction_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(normalize_revise_direction(value).encode("utf-8")).hexdigest()


def sanitize_issue_text(value: str, *, limit: int = 500) -> str:
    """Bound Issue-derived text before it can enter a candidate or audit row.

    Issue projections are already redacted, but an administrator's free-form
    direction is a new input boundary.  Keep the useful repair intent while
    removing credentials, absolute paths and replayable shell commands.
    """

    text = _DIRECTION_SPACE_RE.sub(" ", str(value or "").strip())
    if not text:
        return ""
    try:
        redacted, _report = AuditRedactor().redact({"text": text})
        text = str(redacted["text"])
    except (RedactionError, TypeError, KeyError):
        text = "[REDACTED:UNAVAILABLE]"
    text = _SECRET_ASSIGNMENT_RE.sub("[REDACTED:CREDENTIAL]", text)
    text = _COMMAND_RE.sub("[REDACTED:COMMAND]", text)
    text = _ABSOLUTE_PATH_RE.sub("[REDACTED:PATH]", text)
    return text[:limit]


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
        # Insufficient groups remain in recovery_case_reviews for audit, but
        # they are not Issue cards and cannot trigger QQ notifications.
        if str(review_status) != "passed":
            continue
        try:
            episode_ids = tuple(str(item) for item in json.loads(episode_json or "[]") if str(item))
        except (TypeError, ValueError, json.JSONDecodeError):
            episode_ids = ()
        if not episode_ids:
            continue
        issue_id = _stable_issue_id(str(recovery_key), episode_ids)
        sample = connection.execute(
            """SELECT failure_goal,failure_class,correction_goal,operation_family,failure_family,
                      task_family,correction_family FROM recovery_episodes
               WHERE workspace=? AND episode_id=?""",
            (workspace, episode_ids[0]),
        ).fetchone()
        goal = str(sample[0]) if sample and sample[0] else "未记录任务目标"
        failure_class = str(sample[1]) if sample and sample[1] else "未分类失败"
        correction = str(sample[2]) if sample and sample[2] else "未记录人工纠正"
        operation = str(sample[3]) if sample and sample[3] else "unknown_operation"
        failure_family = str(sample[4]) if sample and sample[4] else "unknown_failure"
        task_family = str(sample[5]) if sample and sample[5] else "unknown_task"
        correction_family = str(sample[6]) if sample and sample[6] else "unknown_correction"
        status = "pending_review"
        recommendation = "可选择记录 Case、写入语义记忆或申请修复候选"
        summary = (
            f"任务：{goal}；失败类别：{failure_class}；"
            f"操作族：{operation}；任务族：{task_family}；异常族：{failure_family}；"
            f"纠正族：{correction_family}；纠正方向：{correction}；质量结论：{str(reason_text)[:240]}"
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
    sql = "SELECT issue_id,title,task_goal,failure_class,correction_goal,summary,status,recommendation,created_at,updated_at,review_reason,episode_ids_json,group_openid FROM failure_issues WHERE workspace=?"
    params: list[Any] = [workspace]
    if values:
        sql += " AND status IN (" + ",".join("?" for _ in values) + ")"
        params.extend(values)
    sql += " ORDER BY created_at DESC,issue_id DESC LIMIT ?"
    params.append(limit)
    rows = connection.execute(sql, params).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            episode_ids = tuple(str(item) for item in json.loads(row[11] or "[]") if str(item))
        except (TypeError, ValueError, json.JSONDecodeError):
            episode_ids = ()
        sample = connection.execute(
            """SELECT operation_family,failure_family,task_family,correction_family
               FROM recovery_episodes WHERE workspace=? AND episode_id=?""",
            (workspace, episode_ids[0]),
        ).fetchone() if episode_ids else None
        result.append({
            "issue_id": str(row[0]), "title": str(row[1]), "task_goal": str(row[2] or ""),
            "failure_class": str(row[3]), "correction_goal": str(row[4] or ""),
            "summary": str(row[5]), "status": str(row[6]), "recommendation": str(row[7]),
            "created_at": str(row[8]), "updated_at": str(row[9]), "review_reason": str(row[10] or ""),
            "group_openid": str(row[12]) if row[12] else None,
            "operation_family": str(sample[0]) if sample and sample[0] else "unknown_operation",
            "failure_family": str(sample[1]) if sample and sample[1] else "unknown_failure",
            "task_family": str(sample[2]) if sample and sample[2] else "unknown_task",
            "correction_family": str(sample[3]) if sample and sample[3] else "unknown_correction",
        })
    return tuple(result)


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
    issue = dict(issue)
    for field, limit in (("task_goal", 360), ("failure_class", 120), ("correction_goal", 500), ("review_reason", 500)):
        issue[field] = sanitize_issue_text(str(issue.get(field) or ""), limit=limit)
    note = sanitize_issue_text(note, limit=500)
    memory_id = f"recovery-memory:{hashlib.sha256(issue_id.encode()).hexdigest()[:24]}"
    title = f"失败恢复规则：{str(issue.get('task_goal') or '未命名任务')[:120]}"
    summary = (
        f"失败类别：{str(issue.get('failure_class') or '未分类')[:120]}；"
        f"纠正方向：{str(issue.get('correction_goal') or '未记录')[:240]}；"
        f"人工边界：{str(note or issue.get('review_reason') or '未补充')[:300]}"
    )
    content = (
        "适用场景：\n"
        f"- 任务：{str(issue.get('task_goal') or '未记录')[:360]}\n"
        f"- 失败类别：{str(issue.get('failure_class') or '未分类')[:120]}\n\n"
        "恢复规则：\n"
        f"- {str(issue.get('correction_goal') or '按人工纠正方向处理')[:360]}\n"
        f"- 管理员补充方向：{str(note or issue.get('review_reason') or '未补充')[:360]}\n"
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


def stage_failure_issue_candidate(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    issue: dict[str, Any],
    actor: str,
    feedback: str = "",
    commit: bool = True,
) -> str:
    """Create an isolated repair Skill revision for the M15 evaluator."""

    issue_id = str(issue.get("issue_id") or "").strip()
    if not issue_id or not workspace or not actor:
        raise ValueError("issue, workspace and actor are required")
    candidate_id = f"failure-candidate:{hashlib.sha256(issue_id.encode()).hexdigest()[:24]}"
    safe_issue = dict(issue)
    for field, limit in (("task_goal", 360), ("failure_class", 120), ("correction_goal", 500), ("review_reason", 500)):
        safe_issue[field] = sanitize_issue_text(str(issue.get(field) or ""), limit=limit)
    feedback = sanitize_issue_text(feedback, limit=500)
    suffix = hashlib.sha256((issue_id + str(safe_issue.get("correction_goal") or "")).encode()).hexdigest()[:12]
    skill_name = f"recovery-{suffix}"
    skill_id = f"skill:{skill_name}"
    task_goal = sanitize_issue_text(str(safe_issue.get("task_goal") or "未命名任务"), limit=120)
    task_goal_detail = sanitize_issue_text(str(safe_issue.get("task_goal") or "未记录"), limit=360)
    failure_class = sanitize_issue_text(str(safe_issue.get("failure_class") or "未分类"), limit=120)
    correction_goal = sanitize_issue_text(
        str(safe_issue.get("correction_goal") or "遵循人工纠正方向"), limit=500
    )
    confirmed_direction = sanitize_issue_text(
        str(feedback or safe_issue.get("review_reason") or "未补充"), limit=500
    )
    content = (
        f"# 失败恢复：{task_goal}\n\n"
        "## 适用场景\n"
        f"- 任务目标：{task_goal_detail}\n"
        f"- 失败类别：{failure_class}\n\n"
        "## 恢复规则\n"
        f"- {correction_goal}\n"
        f"- 管理员确认方向：{confirmed_direction}\n"
        "- 仅适用于同类任务；范围不一致、敏感请求或需要外部副作用时转人工确认。\n"
        "- 不回显凭据、成员身份、完整对话、完整工具参数或隐藏推理。\n\n"
        "## 验证边界\n"
        "- 必须先通过 M15 的基线/候选 A/B、反例、安全和性能门禁。\n"
        "- 本候选不会改变当前生效 Skill，也不会自动创建 Proposal、PR 或发布。\n"
    )
    candidate_hash = digest(content)
    baseline_id = f"{candidate_id}:baseline"
    revision_id = f"{candidate_id}:revision:{candidate_hash[7:19]}"
    now = _now()
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """INSERT INTO skills(skill_id,namespace,name,source_kind,current_version,status,description,
               tool_policy_json,references_json,created_at,updated_at)
               VALUES(?,?,?,'workspace',NULL,'staging',?,'{}','[]',?,?)
               ON CONFLICT(skill_id) DO NOTHING""",
            (skill_id, "skill-evolution", skill_name, "失败恢复候选（待 M15 评测）", now, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO skill_revisions
               (revision_id,skill_id,skill_version,content_hash,content,source_case_ids_json,
                author_actor,status,created_at)
               VALUES(?,?,?,?,?,?,'failure-issue','staging',?)""",
            (baseline_id, skill_id, "0", digest(""), "", json.dumps([issue_id]), now),
        )
        connection.execute(
            "UPDATE skills SET current_revision_id=?,current_version='0' "
            "WHERE skill_id=? AND current_revision_id IS NULL",
            (baseline_id, skill_id),
        )
        connection.execute(
            """INSERT OR IGNORE INTO skill_revisions
               (revision_id,skill_id,skill_version,content_hash,content,source_case_ids_json,
                author_actor,status,created_at)
               VALUES(?,?,?,?,?,?,'failure-issue','staging',?)""",
            (revision_id, skill_id, "candidate", candidate_hash, content, json.dumps([issue_id]), now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO failure_issue_candidates
               (candidate_id,issue_id,workspace,skill_id,skill_name,baseline_revision_id,
                candidate_revision_id,candidate_hash,candidate_content,status,reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'queued','等待 M15 隔离 A/B 评测',?,?)""",
            (candidate_id, issue_id, workspace, skill_id, skill_name, baseline_id, revision_id,
             candidate_hash, content, now, now),
        )
        if owns_transaction and commit:
            connection.commit()
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise
    return candidate_id


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
    candidate_enabled: bool = True,
) -> dict[str, Any]:
    """Apply one administrator routing action with durable idempotency.

    ``revise`` is deliberately implemented as a single transaction.  It
    writes the internal note, the request, and the isolated candidate before
    changing the Issue state, so a failure rolls back every side effect.
    """

    if action not in ISSUE_ACTIONS:
        raise ValueError(f"unsupported failure issue action: {action}")
    if not actor_openid or not group_openid:
        raise ValueError("actor and group are required")
    if action == "reject" and not str(note or "").strip():
        raise ValueError("拒绝原因不能为空")
    issue = get_failure_issue(connection, workspace=workspace, issue_id=issue_id)
    if issue is None:
        raise LookupError("failure issue not found")
    direction = normalize_revise_direction(note)
    direction_hash = revise_direction_hash(note) if action == "revise" else None
    key = (
        f"failure-issue:{issue_id}:{actor_openid}:{direction_hash}"
        if action == "revise"
        else idempotency_key or f"failure-issue:{issue_id}:{action}:{actor_openid}:{hashlib.sha256(direction.encode()).hexdigest()[:12]}"
    )
    existing = connection.execute(
        "SELECT action_id,result_status,request_id,candidate_id,direction_hash "
        "FROM failure_issue_actions WHERE idempotency_key=?", (key,)
    ).fetchone()
    if existing:
        existing_group = connection.execute(
            "SELECT group_openid FROM failure_issue_actions WHERE action_id=?", (existing[0],)
        ).fetchone()
        if existing_group is not None and str(existing_group[0]) != group_openid:
            raise ValueError("Issue 操作不属于当前 QQ 群")
        return {
            "issue_id": issue_id,
            "status": str(existing[1]),
            "idempotent": True,
            "action_id": str(existing[0]),
            "request_id": existing[2],
            "candidate_id": existing[3],
            "direction_hash": existing[4],
        }

    if issue.get("group_openid") and issue["group_openid"] != group_openid:
        raise ValueError("Issue 不属于当前 QQ 群")
    if action == "revise" and str(issue.get("recommendation") or "").startswith("证据不足"):
        raise ValueError("recovery evidence is insufficient for a repair candidate")
    current = issue["status"]
    if action == "revise":
        if not direction:
            raise ValueError("修复方向不能为空")
        if not candidate_enabled:
            raise ValueError("修复 Skill 候选当前关闭")
        previous = connection.execute(
            "SELECT direction_hash FROM failure_issue_actions WHERE issue_id=? AND action='revise' "
            "AND direction_hash IS NOT NULL LIMIT 1", (issue_id,)
        ).fetchone()
        if previous is not None and str(previous[0]) != direction_hash:
            raise ValueError("该 Issue 已提交不同修复方向，请人工复核或创建新 Issue")
        if current != "pending_review":
            raise ValueError(f"issue status does not accept action: {current}")
        next_status = "candidate_requested"
    elif action == "note":
        if current != "pending_review":
            raise ValueError(f"issue status does not accept action: {current}")
        next_status = current
    elif current != "pending_review":
        raise ValueError(f"issue status does not accept action: {current}")
    elif action == "record_case":
        next_status = "recorded_case"
    elif action == "record_memory":
        next_status = "memory_recorded"
    elif action == "request_candidate":
        if not candidate_enabled:
            raise ValueError("修复 Skill 候选当前关闭")
        if issue["recommendation"].startswith("证据不足"):
            raise ValueError("recovery evidence is insufficient for a repair candidate")
        feedback_row = connection.execute(
            """SELECT note FROM failure_issue_actions
               WHERE issue_id=? AND action='note' AND trim(note)<>''
               ORDER BY created_at DESC LIMIT 1""", (issue_id,)
        ).fetchone()
        if not note.strip() and not (feedback_row and str(feedback_row[0]).strip()):
            raise ValueError("请先用 note 提交人工修复方向，再申请修复候选")
        next_status = "candidate_requested"
    else:
        next_status = "rejected"

    now = _now()
    action_id = f"failure-action:{uuid4()}"
    request_id: str | None = None
    candidate_id: str | None = None
    safe_note = sanitize_issue_text(note, limit=1000)
    connection.execute("BEGIN IMMEDIATE")
    try:
        if not issue.get("group_openid"):
            connection.execute(
                "UPDATE failure_issues SET group_openid=? WHERE issue_id=? AND workspace=? AND group_openid IS NULL",
                (group_openid, issue_id, workspace),
            )
        if action == "revise":
            request_id = f"request-candidate:{uuid4()}"
            connection.execute(
                "INSERT INTO failure_issue_actions "
                "(action_id,issue_id,workspace,actor_openid,group_openid,action,note,result_status,"
                "idempotency_key,direction_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"failure-action:{uuid4()}", issue_id, workspace, actor_openid, group_openid,
                 "note", safe_note, next_status, f"{key}:note", direction_hash, now),
            )
            candidate_id = stage_failure_issue_candidate(
                connection, workspace=workspace, issue=issue, actor=actor_openid,
                feedback=safe_note, commit=False,
            )
            connection.execute(
                "INSERT INTO failure_issue_actions "
                "(action_id,issue_id,workspace,actor_openid,group_openid,action,note,result_status,"
                "idempotency_key,request_id,candidate_id,direction_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, issue_id, workspace, actor_openid, group_openid, "request_candidate", safe_note,
                 next_status, f"{key}:request_candidate", request_id, candidate_id, direction_hash, now),
            )
        connection.execute(
            """INSERT INTO failure_issue_actions
               (action_id,issue_id,workspace,actor_openid,group_openid,action,note,result_status,idempotency_key,
                request_id,candidate_id,direction_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (action_id, issue_id, workspace, actor_openid, group_openid, action, safe_note, next_status, key,
             request_id, candidate_id, direction_hash, now),
        )
        connection.execute(
            """UPDATE failure_issues SET status=?,updated_at=?,reviewed_at=?,reviewed_by=?,review_reason=?
               WHERE issue_id=? AND workspace=? AND status=?""",
            (next_status, now, now if action != "note" else issue["updated_at"], actor_openid,
            safe_note if note else issue["review_reason"], issue_id, workspace, current),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError("Issue 状态已变化，请重试或人工复核")
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    if action == "record_memory":
        record_failure_issue_memory(connection, workspace=workspace, issue=issue, actor=actor_openid, note=note)
    if action == "request_candidate":
        feedback = sanitize_issue_text(
            note.strip() or (str(feedback_row[0]).strip() if feedback_row else ""), limit=500
        )
        stage_failure_issue_candidate(
            connection, workspace=workspace, issue=issue, actor=actor_openid, feedback=feedback
        )
        candidate_id = connection.execute(
            "SELECT candidate_id FROM failure_issue_candidates WHERE issue_id=?", (issue_id,)
        ).fetchone()[0]
    return {
        "issue_id": issue_id, "status": next_status, "idempotent": False,
        "action_id": action_id, "request_id": request_id, "candidate_id": candidate_id,
        "direction_hash": direction_hash,
    }


__all__ = [
    "ISSUE_ACTIONS", "MUTATING_ACTIONS", "apply_failure_issue_action", "create_failure_issues",
    "get_failure_issue", "list_failure_issues", "normalize_revise_direction", "record_failure_issue_memory",
    "revise_direction_hash", "sanitize_issue_text",
    "stage_failure_issue_candidate",
]
