"""Redacted, conservative recovery-case evidence for M13.

An episode is deliberately only a link between a failed user turn and its
next successful, same-session correction.  It is not a Skill candidate and
never changes an Agent context, a Proposal, or a remote repository.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable
from uuid import uuid4

from nanobot.memory.semantic_evidence import SemanticProjection, project_user_task

_CLASSIFICATION_VERSION = "m17-v1"
_FILENAME_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9]+")
_DATE_RE = re.compile(r"\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|20\d{6}|m\d+)\b", re.I)

@dataclass(frozen=True, slots=True)
class RecoveryEpisode:
    episode_id: str
    failure_trace_id: str
    recovery_trace_id: str | None
    status: str


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(UTC).isoformat(timespec="seconds")


def _tools(tools: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(tool).strip() for tool in tools if str(tool).strip()}))


def _operation_family(tools: Iterable[str]) -> str:
    names = set(_tools(tools))
    if names & {"exec", "write_stdin", "run_cli_app"}:
        return "process_execute"
    if names & {"write_file", "edit_file", "apply_patch"}:
        return "file_write"
    if "message" in names:
        return "external_message"
    if "cron" in names:
        return "schedule_change"
    if names & {"web_fetch", "web_search"}:
        return "external_read"
    if "read_file" in names:
        return "skill_read" if "skill_read" in names else "file_read"
    if "list_dir" in names:
        return "directory_list"
    if "skill_catalog_search" in names:
        return "skill_search"
    if "skill_read" in names:
        return "skill_read"
    return "unknown_operation"


def _failure_family(
    *,
    error_code: str | None = None,
    error_type: str | None = None,
    error_source: str | None = None,
    detail: str | None = None,
    stop_reason: str | None = None,
) -> str:
    value = " ".join(str(item or "") for item in (error_code, error_type, error_source, detail)).casefold()
    if "file_not_found" in value or "filenotfound" in value or "file not found" in value:
        return "resource_not_found"
    if "permission" in value or "access_denied" in value or "forbidden" in value:
        return "permission_denied"
    if "invalid_tool_arguments" in value or "validationerror" in value or "invalid argument" in value:
        return "invalid_arguments"
    if "timeout" in value or "timed out" in value:
        return "timeout"
    if "rate_limit" in value or "rate limit" in value or "429" in value:
        return "rate_limited"
    if "policy_blocked" in value or "policyerror" in value or "blocked" in value:
        return "policy_blocked"
    if stop_reason == "tool_error":
        return "tool_exception"
    if stop_reason == "error":
        return "agent_exception"
    return "unknown_failure"


def _task_family(text: str, tools: Iterable[str]) -> str:
    value = str(text or "").casefold()
    operation = _operation_family(tools)
    if operation == "process_execute":
        return "process_execution"
    if operation == "file_write":
        return "workspace_file_change"
    if operation in {"external_message", "schedule_change", "external_read"}:
        return operation
    if operation == "skill_search" or "skill" in value and any(token in value for token in ("找", "查", "搜", "目录")):
        return "skill_discovery"
    if operation == "skill_read" or "skill" in value and any(token in value for token in ("读取", "查看", "内容")):
        return "skill_content_lookup"
    if operation == "directory_list" or any(token in value for token in ("目录", "文件夹", "有哪些文件")):
        return "workspace_directory_inventory"
    if operation == "file_read":
        return "workspace_file_lookup"
    return "unknown_task"


def _correction_family(text: str) -> str:
    value = str(text or "").casefold()
    if any(token in value for token in ("路径", "文件名", "改成", "写错", "重新读取", "换成")):
        return "correct_path"
    if any(token in value for token in ("skill 名称", "技能名称", "skill名", "名称写错")):
        return "correct_skill_name"
    if any(token in value for token in ("参数", "补充参数", "参数不完整")):
        return "correct_arguments"
    if any(token in value for token in ("范围", "只看", "限定", "缩小")):
        return "narrow_scope"
    if any(token in value for token in ("改为", "换一个", "换目标")):
        return "change_target"
    return "general_correction"


def _normalised_goal(value: str) -> str:
    """Normalize volatile names for diagnostics without storing a new transcript."""

    text = _FILENAME_RE.sub("<file>", str(value or ""))
    text = _DATE_RE.sub("<id>", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _projection(text: str, tools: Iterable[str]) -> tuple[SemanticProjection | None, str | None]:
    """Reuse M12's redaction boundary; no raw user text reaches this module."""

    verified = _tools(tools)
    if not verified:
        return None, "unsafe_or_unverified_recovery_operation"
    return project_user_task(text, verified)


def record_failed_episode(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    trace_id: str,
    session_key: str,
    source_type: str,
    stop_reason: str,
    user_text: str,
    tools: Iterable[str],
    failure_error_code: str | None = None,
    failure_error_type: str | None = None,
    failure_error_source: str | None = None,
    failure_retryability: str | None = None,
    failure_detail: str | None = None,
) -> RecoveryEpisode | None:
    """Store one eligible failed user turn, or a bounded insufficiency record.

    Failures without a verified operation, system turns, commands and unsafe
    text are intentionally not guessed into recovery lessons.
    """

    if source_type != "user" or stop_reason not in {"error", "tool_error"} or not trace_id:
        return None
    projection, reason = _projection(user_text, tools)
    timestamp = _iso()
    status = "failed" if projection is not None else "insufficient_recovery_evidence"
    failure_class = "tool_execution_error" if stop_reason == "tool_error" else "agent_execution_error"
    operation_family = _operation_family(tools)
    failure_family = _failure_family(
        error_code=failure_error_code,
        error_type=failure_error_type,
        error_source=failure_error_source,
        detail=failure_detail,
        stop_reason=stop_reason,
    )
    task_family = _task_family(projection.task_goal if projection else user_text, tools)
    episode_id = f"recovery:{uuid4()}"
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """INSERT INTO recovery_episodes
               (episode_id,workspace,session_key,failure_trace_id,failure_goal,failure_intent,failure_class,
                failure_tools_json,operation_family,failure_family,task_family,correction_family,
                classification_version,failure_error_code,failure_error_type,failure_error_source,
                failure_retryability,redaction_status,status,rejection_reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'safe',?,?,?,?)
               ON CONFLICT(failure_trace_id) DO UPDATE SET
                 failure_goal=excluded.failure_goal,failure_intent=excluded.failure_intent,
                 failure_tools_json=excluded.failure_tools_json,
                 status=CASE WHEN recovery_episodes.status='recovered' THEN 'recovered' ELSE excluded.status END,
                 operation_family=excluded.operation_family,failure_family=excluded.failure_family,
                 task_family=excluded.task_family,classification_version=excluded.classification_version,
                 failure_error_code=excluded.failure_error_code,failure_error_type=excluded.failure_error_type,
                 failure_error_source=excluded.failure_error_source,failure_retryability=excluded.failure_retryability,
                 rejection_reason=excluded.rejection_reason,updated_at=excluded.updated_at""",
            (
                episode_id,
                workspace,
                session_key,
                trace_id,
                projection.task_goal if projection else None,
                projection.intent if projection else None,
                failure_class,
                json.dumps(list(_tools(tools)), ensure_ascii=False),
                operation_family,
                failure_family,
                task_family,
                "unknown_correction",
                _CLASSIFICATION_VERSION,
                failure_error_code,
                failure_error_type,
                failure_error_source,
                failure_retryability,
                status,
                reason,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    row = connection.execute(
        "SELECT episode_id,failure_trace_id,recovery_trace_id,status FROM recovery_episodes WHERE failure_trace_id=?",
        (trace_id,),
    ).fetchone()
    return RecoveryEpisode(*row) if row else None


def link_successful_correction(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    trace_id: str,
    session_key: str,
    source_type: str,
    user_text: str,
    tools: Iterable[str],
    max_age_minutes: int = 120,
) -> RecoveryEpisode | None:
    """Link only the latest same-session eligible failure to a success.

    This deliberately refuses broad temporal correlation. Episodes remain
    source facts for L1 projection and may come from one long-lived session.
    """

    if source_type != "user" or not trace_id or not session_key:
        return None
    projection, _reason = _projection(user_text, tools)
    if projection is None:
        return None
    existing = connection.execute(
        "SELECT episode_id,failure_trace_id,recovery_trace_id,status FROM recovery_episodes "
        "WHERE workspace=? AND session_key=? AND recovery_trace_id=? AND status='recovered'",
        (workspace, session_key, trace_id),
    ).fetchone()
    if existing is not None:
        # Replaying the same completed correction must retain its original
        # association, including when a later derived settlement needs retrying.
        return RecoveryEpisode(*existing)
    cutoff = _iso(_now() - timedelta(minutes=max_age_minutes))
    row = connection.execute(
        """SELECT episode_id,failure_trace_id,recovery_trace_id,status,failure_intent,task_family,operation_family
           FROM recovery_episodes
           WHERE workspace=? AND session_key=? AND status='failed' AND created_at>=?
           ORDER BY created_at DESC LIMIT 1""",
        (workspace, session_key, cutoff),
    ).fetchone()
    if row is None:
        return None
    # A correction must remain in the same broad task family.  This prevents
    # an unrelated next question from turning a transient failure into a case.
    if str(row[4] or "") != projection.intent:
        return None
    if str(row[5] or "") not in {"", _task_family(projection.task_goal, tools)}:
        return None
    if str(row[6] or "") not in {"", _operation_family(tools)}:
        return None
    timestamp = _iso()
    connection.execute("BEGIN IMMEDIATE")
    try:
        updated = connection.execute(
            """UPDATE recovery_episodes SET recovery_trace_id=?,correction_goal=?,correction_intent=?,
               correction_family=?,recovery_tools_json=?,status='recovered',rejection_reason=NULL,
               recovered_at=?,updated_at=?
               WHERE episode_id=? AND status='failed' AND recovery_trace_id IS NULL""",
            (
                trace_id,
                projection.task_goal,
                projection.intent,
                _correction_family(projection.task_goal),
                json.dumps(list(_tools(tools)), ensure_ascii=False),
                timestamp,
                timestamp,
                str(row[0]),
            ),
        ).rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if not updated:
        return None
    return RecoveryEpisode(str(row[0]), str(row[1]), trace_id, "recovered")


__all__ = ["RecoveryEpisode", "record_failed_episode", "link_successful_correction"]
