"""Read committed Audit evidence and maintain the workspace trace index.

The Audit JSONL/catalog remains the source of truth.  This module only derives
small, redacted records for the workspace memory database; payload records are
never copied into that database.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from nanobot.audit.reader import AuditDiagnostic, AuditReader, ProcessReadResult
from nanobot.audit.redaction import AuditRedactor, RedactionError
from nanobot.audit.schema import AuditEventBase, AuditPayloadBase
from nanobot.memory.db import connect_memory_db
from nanobot.memory.outbox import enqueue_outbox

TRACE_RETENTION_DAYS = 18
PAYLOAD_RETENTION_DAYS = 7
REDACTION_VERSION = "audit-redactor-v1"


class TraceRedactionError(RuntimeError):
    """Raised when a trace cannot be safely summarized."""


@dataclass(frozen=True, slots=True)
class AuditTrace:
    trace_id: str
    process_instance_id: str
    events: tuple[AuditEventBase, ...]
    diagnostics: tuple[AuditDiagnostic, ...]
    source_path: str
    last_committed_epoch: int
    payloads: tuple[AuditPayloadBase, ...] = ()

    @property
    def committed(self) -> bool:
        return not any(d.code in {"catalog_root_invalid", "catalog_fork"} for d in self.diagnostics)


@dataclass(frozen=True, slots=True)
class TraceSummary:
    trace_id: str
    workspace: str
    root_run_id: str | None
    session_key: str | None
    started_at: str
    ended_at: str | None
    outcome: str
    event_count: int
    tool_count: int
    summary: str
    summary_hash: str
    event_cursor: str
    payload_expire_at: str
    trace_expire_at: str
    source_path: str
    redaction_version: str = REDACTION_VERSION


@dataclass(frozen=True, slots=True)
class TraceIndexFailure:
    """A recoverable derived-index error which never changes Audit evidence."""

    trace_id: str
    workspace: str
    source_path: str
    error_code: str
    event_cursor: str | None = None


def iter_audit_traces(audit_root: Path) -> Iterable[AuditTrace]:
    """Yield traces from each process' catalog-committed event prefix."""

    reader = AuditReader(Path(audit_root))
    for process_id in reader.process_ids():
        result: ProcessReadResult = reader.read_process(process_id)
        grouped: dict[str, list[AuditEventBase]] = {}
        payloads_by_event = {payload.event_id: payload for payload in result.payloads}
        for event in result.events:
            if event.trace_id:
                grouped.setdefault(event.trace_id, []).append(event)
        source = str(Path(audit_root).resolve())
        for trace_id, events in sorted(grouped.items()):
            events.sort(key=lambda event: (event.occurred_at, event.durability_epoch, event.event_id))
            yield AuditTrace(
                trace_id=trace_id,
                process_instance_id=process_id,
                events=tuple(events),
                diagnostics=result.diagnostics,
                source_path=source,
                last_committed_epoch=result.last_committed_epoch,
                payloads=tuple(
                    payloads_by_event[event.event_id]
                    for event in events
                    if event.event_id in payloads_by_event
                ),
            )


def _event_outcome(events: tuple[AuditEventBase, ...]) -> str:
    finished = [event for event in events if event.event_type == "run_finished"]
    if finished:
        return str(getattr(finished[-1], "status", "unknown"))
    turns = [event for event in events if event.event_type == "turn_finished"]
    if turns:
        return str(getattr(turns[-1], "status", "unknown"))
    return "running"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _model_json(value: Any) -> dict[str, Any]:
    """Serialize Audit Pydantic records and lightweight test doubles alike."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raw = {
        key: (item.value if hasattr(item, "value") else item)
        for key, item in vars(value).items()
        if not key.startswith("_")
    }
    return json.loads(
        json.dumps(raw, default=lambda item: item.isoformat() if hasattr(item, "isoformat") else str(item))
    )


def summarize_trace(
    trace: AuditTrace,
    *,
    workspace: str,
    redactor: AuditRedactor | None = None,
    now: datetime | None = None,
) -> TraceSummary:
    """Create a deterministic, payload-free and redacted trace summary."""

    if not trace.events:
        raise TraceRedactionError("trace has no committed events")
    redactor = redactor or AuditRedactor()
    events = trace.events
    started = min(event.occurred_at for event in events)
    ended = max(event.occurred_at for event in events)
    try:
        # Audit evidence can contain payloads, but derived memory must never retain them.
        # Redact every committed record first, then discard payloads and keep only summary metadata.
        cleaned_events = [redactor.redact(_model_json(event))[0] for event in events]
        for payload in trace.payloads:
            redactor.redact(_model_json(payload))
        tool_names = sorted(
            {
                str(event["tool_name"])
                for event in cleaned_events
                if event.get("tool_name")
            }
        )
        event_types = sorted({str(event["event_type"]) for event in cleaned_events})
        failed = sorted(
            str(event["error_summary"])
            for event in cleaned_events
            if event.get("error_summary")
        )
        raw = {
            "event_count": len(cleaned_events),
            "event_types": event_types,
            "tool_names": tool_names,
            "outcome": _event_outcome(events),
            "failed": failed,
        }
        cleaned, _ = redactor.redact(raw)
    except RedactionError as error:
        raise TraceRedactionError("trace summary redaction failed") from error
    summary = _canonical_json(cleaned)
    digest = "sha256:" + hashlib.sha256(summary.encode("utf-8")).hexdigest()
    reference = f"{trace.process_instance_id}:{trace.last_committed_epoch}:{events[-1].event_id}"
    del now  # Retention starts when the trace was created, never when it is indexed.
    payload_expire = started + timedelta(days=PAYLOAD_RETENTION_DAYS)
    trace_expire = started + timedelta(days=TRACE_RETENTION_DAYS)
    return TraceSummary(
        trace_id=trace.trace_id,
        workspace=str(Path(workspace).resolve()),
        root_run_id=next(
            (
                str(event["run_id"])
                for event in cleaned_events
                if event.get("parent_run_id") is None and event.get("run_id")
            ),
            None,
        ),
        session_key=next(
            (str(event["session_key"]) for event in cleaned_events if event.get("session_key")),
            None,
        ),
        started_at=started.astimezone(UTC).isoformat(),
        ended_at=ended.astimezone(UTC).isoformat(),
        outcome=_event_outcome(events),
        event_count=len(events),
        tool_count=len(tool_names),
        summary=summary,
        summary_hash=digest,
        event_cursor=reference,
        payload_expire_at=payload_expire.astimezone(UTC).isoformat(),
        trace_expire_at=trace_expire.astimezone(UTC).isoformat(),
        source_path=trace.source_path,
    )


def index_trace_summary(connection: Any, summary: TraceSummary) -> bool:
    """Idempotently upsert one derived trace summary.

    Returns ``True`` when the row changed and ``False`` for an identical replay.
    """

    existing = connection.execute(
        "SELECT summary_hash,event_cursor FROM trace_index WHERE trace_id=?", (summary.trace_id,)
    ).fetchone()
    if existing and existing[0] == summary.summary_hash and existing[1] == summary.event_cursor:
        return False
    connection.execute(
        """INSERT INTO trace_index
        (trace_id,workspace,root_run_id,session_key,started_at,ended_at,outcome,event_count,
         tool_count,summary,summary_hash,redaction_version,event_cursor,payload_expire_at,
         trace_expire_at,source_path,index_status,last_error,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(trace_id) DO UPDATE SET workspace=excluded.workspace,
          root_run_id=excluded.root_run_id,session_key=excluded.session_key,
          started_at=excluded.started_at,ended_at=excluded.ended_at,outcome=excluded.outcome,
          event_count=excluded.event_count,tool_count=excluded.tool_count,summary=excluded.summary,
          summary_hash=excluded.summary_hash,redaction_version=excluded.redaction_version,
          event_cursor=excluded.event_cursor,payload_expire_at=excluded.payload_expire_at,
          trace_expire_at=excluded.trace_expire_at,source_path=excluded.source_path,
          index_status=excluded.index_status,last_error=excluded.last_error,updated_at=excluded.updated_at""",
        (
            summary.trace_id,
            summary.workspace,
            summary.root_run_id,
            summary.session_key,
            summary.started_at,
            summary.ended_at,
            summary.outcome,
            summary.event_count,
            summary.tool_count,
            summary.summary,
            summary.summary_hash,
            summary.redaction_version,
            summary.event_cursor,
            summary.payload_expire_at,
            summary.trace_expire_at,
            summary.source_path,
            "indexed",
            None,
            summary.started_at,
            summary.ended_at or summary.started_at,
        ),
    )
    return True


def _failure_cursor(trace: AuditTrace) -> str | None:
    if not trace.events:
        return None
    return f"{trace.process_instance_id}:{trace.last_committed_epoch}:{trace.events[-1].event_id}"


def _record_index_failure(connection: Any, failure: TraceIndexFailure) -> None:
    """Persist a payload-free degraded state and an idempotent retry request."""

    now = datetime.now(UTC).isoformat()
    connection.execute(
        """INSERT INTO trace_index
        (trace_id,workspace,started_at,outcome,summary,redaction_version,event_cursor,source_path,
         index_status,last_error,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(trace_id) DO UPDATE SET workspace=excluded.workspace,
          event_cursor=excluded.event_cursor,source_path=excluded.source_path,
          index_status='degraded',last_error=excluded.last_error,updated_at=excluded.updated_at""",
        (
            failure.trace_id,
            failure.workspace,
            now,
            "unknown",
            "",
            REDACTION_VERSION,
            failure.event_cursor,
            failure.source_path,
            "degraded",
            failure.error_code,
            now,
            now,
        ),
    )
    connection.commit()
    retry_hash = "sha256:" + hashlib.sha256(
        _canonical_json(
            {
                "trace_id": failure.trace_id,
                "cursor": failure.event_cursor,
                "error_code": failure.error_code,
            }
        ).encode("utf-8")
    ).hexdigest()
    enqueue_outbox(
        connection,
        workspace=failure.workspace,
        object_type="trace_summary",
        object_id=failure.trace_id,
        content_hash=retry_hash,
        operation="upsert",
        payload={"error_code": failure.error_code, "source_path": failure.source_path},
    )


def update_trace_index(
    audit_root: Path,
    workspace: str | Path,
    *,
    connection: Any | None = None,
    redactor: AuditRedactor | None = None,
) -> int:
    """Incrementally replay committed Audit prefixes into the derived index."""

    return index_audit_root(
        audit_root,
        workspace,
        connection=connection,
        redactor=redactor,
    )


def rebuild_trace_index(
    audit_root: Path,
    workspace: str | Path,
    *,
    connection: Any | None = None,
    redactor: AuditRedactor | None = None,
) -> int:
    """Discard only derived trace rows, then replay the committed Audit prefix."""

    return index_audit_root(
        audit_root,
        workspace,
        connection=connection,
        full_rebuild=True,
        redactor=redactor,
    )


def index_audit_root(
    audit_root: Path,
    workspace: str | Path,
    *,
    connection: Any | None = None,
    full_rebuild: bool = False,
    redactor: AuditRedactor | None = None,
) -> int:
    """Index all committed traces from an Audit root into memory SQLite."""

    owns_connection = connection is None
    connection = connection or connect_memory_db(workspace)
    try:
        if full_rebuild:
            connection.execute("DELETE FROM trace_index WHERE workspace=?", (str(Path(workspace).resolve()),))
        changed = 0
        for trace in iter_audit_traces(Path(audit_root)):
            try:
                summary = summarize_trace(trace, workspace=str(workspace), redactor=redactor)
                changed += int(index_trace_summary(connection, summary))
            except TraceRedactionError:
                _record_index_failure(
                    connection,
                    TraceIndexFailure(
                        trace_id=trace.trace_id,
                        workspace=str(Path(workspace).resolve()),
                        source_path=trace.source_path,
                        error_code="redaction_failed",
                        event_cursor=_failure_cursor(trace),
                    ),
                )
            except Exception:
                connection.rollback()
                _record_index_failure(
                    connection,
                    TraceIndexFailure(
                        trace_id=trace.trace_id,
                        workspace=str(Path(workspace).resolve()),
                        source_path=trace.source_path,
                        error_code="index_failed",
                        event_cursor=_failure_cursor(trace),
                    ),
                )
        connection.commit()
        return changed
    except Exception:
        connection.rollback()
        raise
    finally:
        if owns_connection:
            connection.close()
