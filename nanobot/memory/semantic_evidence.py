"""Bounded business-semantic evidence for controlled Skill evolution.

Trace lifecycle metadata proves that an Agent run happened, but it cannot
describe a reusable user task.  This module persists a small, redacted
projection of an eligible user turn.  It deliberately rejects ambiguous
turns rather than falling back to Audit event JSON or full chat transcripts.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable
from uuid import uuid4

from nanobot.audit.redaction import AuditRedactor, RedactionError

_SPACE_RE = re.compile(r"\s+")
_SECRET_RE = re.compile(r"(?i)(?:api[_ -]?key|token|password|secret)\s*[:=]\s*[^\s,;]+")
_FRAMEWORK_RE = re.compile(
    r"(?i)(?:event_count|event_types|checkpoint(?:_|\b)|turn_(?:started|finished)|"
    r"model_(?:request|response|attempt)|delivery_(?:attempted|finished)|trace_created)"
)
_QUERY_RE = re.compile(r"(?:看看|查看|查询|列出|检索|搜索|有哪些|状态|清单|目录|skill)", re.I)
_DIAGNOSE_RE = re.compile(r"(?:排查|诊断|报错|错误|异常|修复|为什么|原因)", re.I)
_EDIT_RE = re.compile(r"(?:修改|更新|编写|实现|添加|删除|重构|调整)", re.I)
_ORGANIZE_RE = re.compile(r"(?:整理|总结|归纳|分类|对比|分析)", re.I)


@dataclass(frozen=True, slots=True)
class SemanticProjection:
    task_goal: str
    intent: str
    input_scope: str
    expected_outcome: str
    operation_signature: tuple[str, ...]
    semantic_key: str


@dataclass(frozen=True, slots=True)
class SemanticEvidenceRecord:
    evidence_id: str
    trace_id: str
    task_goal: str
    intent: str
    input_scope: str
    expected_outcome: str
    operation_signature: tuple[str, ...]
    semantic_key: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _compact(text: str, limit: int = 360) -> str:
    return _SPACE_RE.sub(" ", text).strip()[:limit]


def _intent(text: str) -> tuple[str, str]:
    if _DIAGNOSE_RE.search(text):
        return "排查问题", "定位原因并给出可核对的处理建议"
    if _EDIT_RE.search(text):
        return "变更实现", "完成明确变更并说明验证结果"
    if _ORGANIZE_RE.search(text):
        return "整理分析", "返回结构化整理或分析结果"
    if _QUERY_RE.search(text):
        return "查询信息", "返回与范围相符的结构化查询结果"
    return "一般任务", "完成用户请求并返回可核对的结果"


def project_user_task(text: str, tools: Iterable[str]) -> tuple[SemanticProjection | None, str | None]:
    """Build one safe, conservative projection without retaining raw input.

    This is intentionally rule based for the first quality gate: a task that
    cannot be explained by a deterministic projection is skipped, not guessed
    by the candidate generator.
    """

    raw = str(text or "").strip()
    if not raw or raw.startswith("/"):
        return None, "not_a_user_task"
    try:
        redacted, _report = AuditRedactor().redact({"task": raw})
    except RedactionError:
        return None, "redaction_failed"
    safe = _compact(_SECRET_RE.sub("[REDACTED:CREDENTIAL]", str(redacted["task"])))
    if not safe or safe.startswith(("{", "[")) or _FRAMEWORK_RE.search(safe):
        return None, "framework_or_nonsemantic_input"
    tool_names = tuple(sorted({str(tool).strip() for tool in tools if str(tool).strip()}))
    if not tool_names:
        return None, "no_verified_operation"
    intent, expected = _intent(safe)
    # The current request is a bounded task goal, not a transcript: it is
    # redacted, whitespace-normalized and capped before persistence.
    goal = safe.rstrip("。！？!? ")
    if len(goal) < 6:
        return None, "task_goal_too_short"
    scope = goal[:180]
    key_source = "|".join((intent, goal.casefold(), ",".join(tool_names)))
    semantic_key = "semantic:" + hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:24]
    return SemanticProjection(goal, intent, scope, expected, tool_names, semantic_key), None


def persist_turn_semantic_evidence(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    trace_id: str,
    turn_id: str,
    session_key: str,
    source_type: str,
    outcome: str,
    user_text: str,
    tools: Iterable[str],
) -> SemanticEvidenceRecord | None:
    """Persist only a redacted semantic projection for a successful user turn."""

    if source_type != "user" or outcome != "success" or not trace_id or not turn_id:
        return None
    projection, reason = project_user_task(user_text, tools)
    timestamp = _now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        if projection is None:
            connection.execute(
                """INSERT INTO semantic_task_evidence
                   (evidence_id,workspace,trace_id,turn_id,session_key,source_type,outcome,
                    operation_signature_json,redaction_status,evidence_status,rejection_reason,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'[]','safe','insufficient_semantic_evidence',?,?,?)
                   ON CONFLICT(trace_id) DO UPDATE SET evidence_status=excluded.evidence_status,
                     rejection_reason=excluded.rejection_reason,updated_at=excluded.updated_at""",
                (f"semantic:{uuid4()}", workspace, trace_id, turn_id, session_key, source_type, outcome,
                 reason or "insufficient_semantic_evidence", timestamp, timestamp),
            )
            connection.commit()
            return None
        evidence_id = f"semantic:{uuid4()}"
        connection.execute(
            """INSERT INTO semantic_task_evidence
               (evidence_id,workspace,trace_id,turn_id,session_key,source_type,outcome,task_goal,intent,
                input_scope,expected_outcome,operation_signature_json,semantic_key,redaction_status,
                evidence_status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'safe','eligible',?,?)
               ON CONFLICT(trace_id) DO UPDATE SET task_goal=excluded.task_goal,intent=excluded.intent,
                 input_scope=excluded.input_scope,expected_outcome=excluded.expected_outcome,
                 operation_signature_json=excluded.operation_signature_json,semantic_key=excluded.semantic_key,
                 redaction_status='safe',evidence_status='eligible',rejection_reason=NULL,
                 updated_at=excluded.updated_at""",
            (evidence_id, workspace, trace_id, turn_id, session_key, source_type, outcome,
             projection.task_goal, projection.intent, projection.input_scope, projection.expected_outcome,
             json.dumps(list(projection.operation_signature), ensure_ascii=False), projection.semantic_key,
             timestamp, timestamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return SemanticEvidenceRecord(evidence_id, trace_id, projection.task_goal, projection.intent,
                                  projection.input_scope, projection.expected_outcome,
                                  projection.operation_signature, projection.semantic_key)


def load_semantic_candidate_records(connection: sqlite3.Connection, workspace: str) -> tuple[dict[str, Any], ...]:
    """Return only eligible business-semantic records for the daily selector."""

    rows = connection.execute(
        """SELECT evidence_id,trace_id,task_goal,intent,input_scope,expected_outcome,
                  operation_signature_json,semantic_key
           FROM semantic_task_evidence
           WHERE workspace=? AND evidence_status='eligible' AND redaction_status='safe'
           ORDER BY created_at""",
        (workspace,),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            tools = tuple(str(item) for item in json.loads(row[6] or "[]") if str(item))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        records.append({
            "evidence_id": str(row[0]), "trace_id": str(row[1]), "summary": str(row[2]),
            "intent": str(row[3]), "input_scope": str(row[4]), "expected_outcome": str(row[5]),
            "tools": tools, "task_key": str(row[7]), "outcome": "success", "semantic": True,
        })
    return tuple(records)


def record_quality_review(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    semantic_key: str,
    evidence_ids: Iterable[str],
    status: str,
    reason_code: str,
    reason_text: str,
) -> None:
    evidence = tuple(sorted(set(str(item) for item in evidence_ids if str(item))))
    connection.execute(
        """INSERT OR IGNORE INTO semantic_quality_reviews
           (review_id,workspace,semantic_key,evidence_ids_json,status,reason_code,reason_text,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (f"quality:{uuid4()}", workspace, semantic_key, json.dumps(evidence), status,
         reason_code, reason_text[:500], _now()),
    )
    connection.commit()


__all__ = [
    "SemanticEvidenceRecord", "SemanticProjection", "load_semantic_candidate_records",
    "persist_turn_semantic_evidence", "project_user_task", "record_quality_review",
]
