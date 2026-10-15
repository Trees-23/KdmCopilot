"""Immutable retrieval telemetry."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
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
