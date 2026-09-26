"""Immutable retrieval telemetry and trace-summary search helpers."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RetrievalEvent:
    session_key: str
    intent: str
    scopes: Any
    result_ids: Any
    injected_ids: Any
    injected_context: Any
    outcome: str
    trace_id: str | None = None
    turn_id: str | None = None
    query: Any = None
    tokens: int = 0
    error_code: str | None = None
    retrieval_id: str | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class TraceSearchResult:
    """The payload-free trace fields safe for memory retrieval clients."""

    trace_id: str
    session_key: str | None
    started_at: str
    ended_at: str | None
    outcome: str
    event_count: int
    tool_count: int
    summary: str


def record_retrieval(connection: Any, event: RetrievalEvent) -> str:
    """Append one retrieval event, returning its stable generated identifier."""

    retrieval_id = event.retrieval_id or str(uuid.uuid4())
    created_at = event.created_at or datetime.now(UTC).isoformat()
    connection.execute(
        """INSERT INTO retrieval_events
        (retrieval_id,trace_id,turn_id,session_key,intent,scopes_json,query_digest,
         result_ids_json,injected_ids_json,injected_context_digest,tokens,outcome,error_code,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            retrieval_id,
            event.trace_id,
            event.turn_id,
            event.session_key,
            event.intent,
            json.dumps(event.scopes, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            _digest(event.query if event.query is not None else ""),
            json.dumps(event.result_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(event.injected_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            _digest(event.injected_context),
            event.tokens,
            event.outcome,
            event.error_code,
            created_at,
        ),
    )
    return retrieval_id


def search_trace_summaries(
    connection: Any,
    *,
    workspace: str,
    session_key: str | None = None,
    outcome: str | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> list[TraceSearchResult]:
    """Return structured trace index rows, excluding degraded/expired records."""

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    clauses = ["workspace=?", "index_status='indexed'", "(trace_expire_at IS NULL OR trace_expire_at > ?)"]
    params: list[Any] = [str(Path(workspace).resolve()), (now or datetime.now(UTC)).isoformat()]
    if session_key is not None:
        clauses.append("session_key=?")
        params.append(session_key)
    if outcome is not None:
        clauses.append("outcome=?")
        params.append(outcome)
    params.append(limit)
    rows = connection.execute(
        "SELECT trace_id,session_key,started_at,ended_at,outcome,event_count,tool_count,summary "
        f"FROM trace_index WHERE {' AND '.join(clauses)} ORDER BY started_at DESC LIMIT ?",
        params,
    )
    return [
        TraceSearchResult(
            trace_id=row[0],
            session_key=row[1],
            started_at=row[2],
            ended_at=row[3],
            outcome=row[4],
            event_count=row[5],
            tool_count=row[6],
            summary=row[7],
        )
        for row in rows
    ]


def retrieval_failure_event(
    *, session_key: str, intent: str, error_code: str, trace_id: str | None = None,
) -> RetrievalEvent:
    """Build the standard non-blocking failure record."""

    return RetrievalEvent(
        session_key=session_key,
        intent=intent,
        scopes=[],
        result_ids=[],
        injected_ids=[],
        injected_context=[],
        outcome="failed",
        trace_id=trace_id,
        error_code=error_code,
    )
