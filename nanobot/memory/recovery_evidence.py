"""Redacted, conservative recovery-case evidence for M13.

An episode is deliberately only a link between a failed user turn and its
next successful, same-session correction.  It is not a Skill candidate and
never changes an Agent context, a Proposal, or a remote repository.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable
from uuid import uuid4

from nanobot.memory.semantic_evidence import SemanticProjection, project_user_task

_RECOVERY_SAFE_TOOLS = frozenset({"read_file", "list_dir", "skill_catalog_search", "skill_read"})

@dataclass(frozen=True, slots=True)
class RecoveryEpisode:
    episode_id: str
    failure_trace_id: str
    recovery_trace_id: str | None
    status: str


@dataclass(frozen=True, slots=True)
class RecoveryReview:
    recovery_key: str
    episode_ids: tuple[str, ...]
    status: str
    reason_code: str


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(UTC).isoformat(timespec="seconds")


def _tools(tools: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(tool).strip() for tool in tools if str(tool).strip()}))


def _projection(text: str, tools: Iterable[str]) -> tuple[SemanticProjection | None, str | None]:
    """Reuse M12's redaction boundary; no raw user text reaches this module."""

    verified = _tools(tools)
    if not verified or any(tool not in _RECOVERY_SAFE_TOOLS for tool in verified):
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
    episode_id = f"recovery:{uuid4()}"
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """INSERT INTO recovery_episodes
               (episode_id,workspace,session_key,failure_trace_id,failure_goal,failure_intent,failure_class,
                failure_tools_json,redaction_status,status,rejection_reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,'safe',?,?,?,?)
               ON CONFLICT(failure_trace_id) DO UPDATE SET
                 failure_goal=excluded.failure_goal,failure_intent=excluded.failure_intent,
                 failure_tools_json=excluded.failure_tools_json,status=excluded.status,
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

    This deliberately refuses broad temporal correlation.  The later M13
    quality review requires repeated recovery episodes before even a read-only
    Case card can be surfaced; the episodes may come from one long-lived
    session such as a QQ group session.
    """

    if source_type != "user" or not trace_id or not session_key:
        return None
    projection, _reason = _projection(user_text, tools)
    if projection is None:
        return None
    cutoff = _iso(_now() - timedelta(minutes=max_age_minutes))
    row = connection.execute(
        """SELECT episode_id,failure_trace_id,recovery_trace_id,status,failure_intent
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
    timestamp = _iso()
    connection.execute("BEGIN IMMEDIATE")
    try:
        updated = connection.execute(
            """UPDATE recovery_episodes SET recovery_trace_id=?,correction_goal=?,correction_intent=?,
               recovery_tools_json=?,status='recovered',rejection_reason=NULL,recovered_at=?,updated_at=?
               WHERE episode_id=? AND status='failed' AND recovery_trace_id IS NULL""",
            (
                trace_id,
                projection.task_goal,
                projection.intent,
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


def _recovery_key(failure_class: str, failure_goal: str, correction_intent: str, tools_json: str) -> str:
    payload = "|".join((failure_class, failure_goal.casefold(), correction_intent, tools_json))
    return "recovery:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def review_recovery_episodes(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    min_episodes: int = 3,
    min_sessions: int = 1,
) -> tuple[RecoveryReview, ...]:
    """Create auditable Case-quality decisions without creating a Skill."""

    rows = connection.execute(
        """SELECT episode_id,session_key,failure_goal,failure_class,correction_intent,recovery_tools_json
           FROM recovery_episodes
           WHERE workspace=? AND status='recovered' AND redaction_status='safe'
           ORDER BY recovered_at,episode_id""",
        (workspace,),
    ).fetchall()
    groups: dict[str, list[tuple[str, str]]] = {}
    for episode_id, session_key, failure_goal, failure_class, correction_intent, tools_json in rows:
        if not failure_goal or not correction_intent:
            continue
        key = _recovery_key(str(failure_class), str(failure_goal), str(correction_intent), str(tools_json))
        groups.setdefault(key, []).append((str(episode_id), str(session_key)))
    reviews: list[RecoveryReview] = []
    timestamp = _iso()
    for key, episodes in groups.items():
        episode_ids = tuple(sorted(item[0] for item in episodes))
        sessions = {item[1] for item in episodes}
        if len(episode_ids) >= min_episodes and len(sessions) >= min_sessions:
            status, code, text = (
                "passed",
                "repeated_verified_recovery",
                "重复的失败、人工纠正与验证成功已形成恢复 Case；未生成 Skill。",
            )
        else:
            status, code, text = (
                "insufficient_recovery_evidence",
                "insufficient_recovery_episodes",
                "恢复 Episode 数量不足；仅保留审计，不生成 Case 卡。",
            )
        connection.execute(
            """INSERT OR IGNORE INTO recovery_case_reviews
               (review_id,workspace,recovery_key,episode_ids_json,status,reason_code,reason_text,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (f"recovery-review:{uuid4()}", workspace, key, json.dumps(episode_ids), status, code, text, timestamp),
        )
        reviews.append(RecoveryReview(key, episode_ids, status, code))
    connection.commit()
    return tuple(reviews)


def list_recovery_reviews(connection: sqlite3.Connection, *, workspace: str, limit: int = 20) -> tuple[dict[str, object], ...]:
    """Read only the Case-card projection permitted to an approval group."""

    if limit < 1 or limit > 100:
        raise ValueError("recovery review list limit must be between 1 and 100")
    rows = connection.execute(
        """SELECT recovery_key,episode_ids_json,status,reason_code,reason_text,created_at
           FROM recovery_case_reviews WHERE workspace=? ORDER BY created_at DESC LIMIT ?""",
        (workspace, limit),
    ).fetchall()
    cards: list[dict[str, object]] = []
    for recovery_key, episode_ids_json, status, reason_code, reason_text, created_at in rows:
        try:
            episode_ids = tuple(str(value) for value in json.loads(episode_ids_json or "[]") if str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            episode_ids = ()
        sample = None
        if episode_ids:
            placeholders = ",".join("?" for _ in episode_ids)
            sample = connection.execute(
                "SELECT failure_goal,failure_class,correction_goal FROM recovery_episodes "
                f"WHERE workspace=? AND episode_id IN ({placeholders}) ORDER BY created_at LIMIT 1",
                (workspace, *episode_ids),
            ).fetchone()
        cards.append({
            "recovery_key": str(recovery_key), "episode_count": len(episode_ids), "status": str(status),
            "reason_code": str(reason_code), "reason_text": str(reason_text), "created_at": str(created_at),
            "failure_goal": str(sample[0]) if sample and sample[0] else "未记录",
            "failure_class": str(sample[1]) if sample and sample[1] else "未记录",
            "correction_goal": str(sample[2]) if sample and sample[2] else "未记录",
        })
    return tuple(cards)


__all__ = [
    "RecoveryEpisode", "RecoveryReview", "link_successful_correction", "list_recovery_reviews",
    "record_failed_episode", "review_recovery_episodes",
]
