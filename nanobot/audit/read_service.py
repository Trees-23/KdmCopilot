"""Indexed, read-only audit queries for interactive WebUI consumers."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from nanobot.audit.integrity import hash_record
from nanobot.audit.redaction import AuditRedactor
from nanobot.audit.schema import AuditEventBase, audit_event_adapter, audit_payload_adapter
from nanobot.audit.types import JsonValue

IndexState = Literal["ready", "building", "stale", "disabled", "unavailable"]
DisplayStatus = Literal[
    "running", "failed", "interrupted", "cancelled", "incomplete", "warning", "succeeded"
]


def _record_value(record: object, field: str) -> object:
    if isinstance(record, dict):
        return record.get(field)
    return getattr(record, field, None)


def expected_delivery_suppression(event: object, trace_events: list[object]) -> bool:
    """Recognize explicit and pre-reason WebUI stream suppression evidence."""
    if (
        _record_value(event, "event_type") != "delivery_finished"
        or _record_value(event, "status") != "suppressed"
    ):
        return False
    reason = _record_value(event, "suppression_reason")
    if reason:
        return reason == "webui_stream_already_delivered"

    run_id = _record_value(event, "run_id")
    if not run_id:
        return False
    webui_source = any(
        _record_value(candidate, "source_type") == "websocket"
        or str(_record_value(candidate, "session_key") or "").startswith("websocket:")
        for candidate in trace_events
    )
    successful_run = any(
        _record_value(candidate, "event_type") == "run_finished"
        and _record_value(candidate, "run_id") == run_id
        and _record_value(candidate, "status") == "succeeded"
        for candidate in trace_events
    )
    return webui_source and successful_run


class SanitizedIndexError(BaseModel):
    code: str
    message: str
    at: datetime | None = None


class IndexStatus(BaseModel):
    state: IndexState
    revision: int | None = None
    coverage_complete: bool = False
    updated_at: datetime | None = None
    lag_ms: int | None = None
    last_error: SanitizedIndexError | None = None


class TraceListFilter(BaseModel):
    since: datetime | None = None
    until: datetime | None = None
    session_key: str | None = None
    source_type: str | None = None
    query: str | None = Field(default=None, max_length=256)
    status: DisplayStatus | None = None
    model: str | None = Field(default=None, max_length=256)
    tool: str | None = Field(default=None, max_length=256)
    anomalies_only: bool = False
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class TraceListItem(BaseModel):
    trace_id: str
    title: str
    source_types: list[str]
    primary_source_type: str
    first_seen: datetime
    last_seen: datetime
    display_status: DisplayStatus
    turn_count: int
    run_count: int
    anomaly_count: int
    integrity_status: str
    active: bool
    session_key: str | None
    event_count: int


class TraceListPage(BaseModel):
    items: list[TraceListItem]
    next_cursor: str | None
    index: IndexStatus


class SessionListFilter(BaseModel):
    query: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class SessionListItem(BaseModel):
    session_key: str
    title: str
    source_types: list[str]
    first_seen: datetime
    last_seen: datetime
    trace_count: int
    active_trace_count: int
    warning_count: int
    error_count: int
    integrity_status: str
    latest_trace_id: str


class SessionListPage(BaseModel):
    items: list[SessionListItem]
    next_cursor: str | None
    index: IndexStatus


class IndexedTraceEvents(BaseModel):
    trace_id: str
    revision: int
    events: list[AuditEventBase]
    integrity_status: str
    integrity_error_codes: list[str]
    integrity_warning_codes: list[str]
    active_run_ids: set[str]


class EventPage(BaseModel):
    events: list[AuditEventBase]
    next_cursor: str | None
    revision: int
    total: int


class PayloadReadResult(BaseModel):
    payload_id: str
    event_id: str | None = None
    payload_kind: str | None = None
    available: bool
    reason: str | None = None
    content: JsonValue | None = None
    truncated: bool = False


class CursorStaleError(ValueError):
    pass


class AuditReadUnavailableError(RuntimeError):
    def __init__(self, status: IndexStatus) -> None:
        super().__init__(status.state)
        self.status = status


class PayloadTooLargeError(ValueError):
    pass


class PayloadLocatorInvalidError(ValueError):
    pass


def _encode_cursor(revision: int, last_seen: str, trace_id: str) -> str:
    raw = json.dumps([2, revision, last_seen, trace_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[int, str, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        version, revision, last_seen, trace_id = json.loads(
            base64.urlsafe_b64decode(padded).decode()
        )
        if version != 2 or not isinstance(revision, int):
            raise ValueError
        return revision, str(last_seen), str(trace_id)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid audit cursor") from error


class AuditReadService:
    def __init__(
        self,
        index_path: Path,
        *,
        status_provider: Callable[[], IndexStatus] | None = None,
        active_run_ids: Callable[[], set[str]] | None = None,
    ) -> None:
        self.index_path = index_path
        self._status_provider = status_provider
        self._active_run_ids = active_run_ids or set

    def status(self) -> IndexStatus:
        if self._status_provider is not None:
            return self._status_provider()
        return self.status_from_index()

    def status_from_index(self) -> IndexStatus:
        if not self.index_path.exists():
            return IndexStatus(state="building")
        with self._connect() as connection:
            meta = dict(connection.execute("SELECT key, value FROM meta"))
        return self._status_from_meta(meta)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.index_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _status_from_meta(meta: dict[str, str]) -> IndexStatus:
        coverage = meta.get("coverage_complete") == "true"
        updated = datetime.fromisoformat(meta["updated_at"]) if meta.get("updated_at") else None
        newest = (
            datetime.fromisoformat(meta["newest_catalog_commit_at"])
            if meta.get("newest_catalog_commit_at")
            else None
        )
        lag_ms = None
        if newest is not None:
            lag_ms = max(0, int((datetime.now(UTC) - newest).total_seconds() * 1000))
        error = None
        if meta.get("last_error_code"):
            error = SanitizedIndexError(
                code=meta["last_error_code"],
                message=meta.get("last_error_message", "Index refresh failed."),
                at=(
                    datetime.fromisoformat(meta["last_error_at"])
                    if meta.get("last_error_at")
                    else None
                ),
            )
        return IndexStatus(
            state="stale" if error or not coverage else "ready",
            revision=int(meta.get("revision", "0")),
            coverage_complete=coverage,
            updated_at=updated,
            lag_ms=lag_ms,
            last_error=error,
        )

    @staticmethod
    def _display_status(
        statuses: set[str], *, active: bool, incomplete: bool, warning: bool
    ) -> DisplayStatus:
        if active:
            return "running"
        if statuses & {"failed", "exhausted"}:
            return "failed"
        if "interrupted" in statuses:
            return "interrupted"
        if "cancelled" in statuses:
            return "cancelled"
        if incomplete:
            return "incomplete"
        if warning:
            return "warning"
        return "succeeded"

    def list_traces(self, filters: TraceListFilter) -> TraceListPage:
        status = self.status()
        if status.state in {"building", "disabled", "unavailable"}:
            raise AuditReadUnavailableError(status)
        revision = status.revision or 0
        clauses = ["e.trace_id IS NOT NULL"]
        params: list[object] = []
        if filters.since:
            clauses.append("e.occurred_at >= ?")
            params.append(filters.since.isoformat())
        if filters.until:
            clauses.append("e.occurred_at <= ?")
            params.append(filters.until.isoformat())
        for field, value in (
            ("session_key", filters.session_key),
            ("source_type", filters.source_type),
        ):
            if value:
                clauses.append(
                    f"e.trace_id IN (SELECT trace_id FROM events WHERE {field}=?)"
                )
                params.append(value)
        if filters.query:
            clauses.append("(LOWER(e.trace_id) LIKE ? OR LOWER(COALESCE(e.session_key,'')) LIKE ?)")
            needle = f"%{filters.query.lower()}%"
            params.extend((needle, needle))
        for field, value in (("model", filters.model), ("tool_name", filters.tool)):
            if value:
                clauses.append(
                    f"e.trace_id IN (SELECT trace_id FROM events WHERE {field}=?)"
                )
                params.append(value)
        cursor_clause = ""
        if filters.cursor:
            cursor_revision, last_seen, trace_id = _decode_cursor(filters.cursor)
            if cursor_revision != revision:
                raise CursorStaleError("audit cursor revision is stale")
            cursor_clause = (
                "HAVING (MAX(e.occurred_at) < ? OR "
                "(MAX(e.occurred_at) = ? AND e.trace_id < ?))"
            )
            params.extend((last_seen, last_seen, trace_id))
        sql = f"""
            SELECT e.trace_id, MIN(e.occurred_at) first_seen, MAX(e.occurred_at) last_seen,
              COUNT(DISTINCT e.turn_id) turn_count, COUNT(DISTINCT e.run_id) run_count,
              COUNT(*) event_count,
              GROUP_CONCAT(DISTINCT e.source_type) source_types, MIN(e.session_key) session_key
            FROM events e WHERE {' AND '.join(clauses)} GROUP BY e.trace_id {cursor_clause}
            ORDER BY last_seen DESC, e.trace_id DESC LIMIT ?
        """
        params.append(filters.limit + 1)
        active_ids = self._active_run_ids()
        items: list[TraceListItem] = []
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            for row in rows[: filters.limit]:
                trace_id = row["trace_id"]
                event_rows = connection.execute(
                    "SELECT event_type, status, run_id, event_json FROM events WHERE trace_id=?",
                    (trace_id,),
                ).fetchall()
                event_documents = [json.loads(value["event_json"]) for value in event_rows]
                statuses = {value["status"] for value in event_rows if value["status"]}
                started = {
                    value["run_id"]
                    for value in event_rows
                    if value["event_type"] == "run_started"
                }
                finished = {
                    value["run_id"]
                    for value in event_rows
                    if value["event_type"] == "run_finished"
                }
                trace_active = any(run_id in active_ids for run_id in started if run_id)
                unknown_suppression = any(
                    document.get("event_type") == "delivery_finished"
                    and document.get("status") == "suppressed"
                    and not expected_delivery_suppression(document, event_documents)
                    for document in event_documents
                )
                anomaly_count = sum(
                    value["event_type"].startswith("orphan_")
                    or value["event_type"] == "audit_degraded"
                    or value["status"] in {"error", "timeout", "blocked"}
                    for value in event_rows
                ) + int(unknown_suppression)
                display = self._display_status(
                    statuses,
                    active=trace_active,
                    incomplete=bool(started - finished),
                    warning=anomaly_count > 0,
                )
                if filters.status and display != filters.status:
                    continue
                if filters.anomalies_only and anomaly_count == 0:
                    continue
                sources = sorted(filter(None, (row["source_types"] or "").split(",")))
                source_rows = connection.execute(
                    """
                    SELECT source_type FROM events WHERE trace_id=? AND source_type IS NOT NULL
                    ORDER BY occurred_at, process_instance_id, durability_epoch, segment_id,
                      segment_sequence, event_id
                    """,
                    (trace_id,),
                ).fetchall()
                inbound = {"user", "websocket"}
                primary = next(
                    (value[0] for value in source_rows if value[0] in inbound),
                    next(
                        (value[0] for value in source_rows if value[0] not in {"system", "delivery"}),
                        source_rows[0][0] if source_rows else "system",
                    ),
                )
                integrity_rows = connection.execute(
                    """
                    SELECT DISTINCT p.status FROM process_integrity p JOIN events x
                      ON x.process_instance_id=p.process_instance_id WHERE x.trace_id=?
                    """,
                    (trace_id,),
                ).fetchall()
                integrity = "valid"
                for candidate in ("invalid", "unknown", "incomplete", "degraded"):
                    if any(value[0] == candidate for value in integrity_rows):
                        integrity = candidate
                        break
                items.append(
                    TraceListItem(
                        trace_id=trace_id,
                        title=(
                            f"{primary} / "
                            f"{datetime.fromisoformat(row['last_seen']).astimezone():%H:%M} / "
                            f"{trace_id[:8]}"
                        ),
                        source_types=sources,
                        primary_source_type=primary,
                        first_seen=row["first_seen"],
                        last_seen=row["last_seen"],
                        display_status=display,
                        turn_count=row["turn_count"],
                        run_count=row["run_count"],
                        anomaly_count=anomaly_count,
                        integrity_status=integrity,
                        active=trace_active,
                        session_key=row["session_key"],
                        event_count=row["event_count"],
                    )
                )
        next_cursor = None
        if len(rows) > filters.limit and items:
            last = items[-1]
            next_cursor = _encode_cursor(revision, last.last_seen.isoformat(), last.trace_id)
        return TraceListPage(items=items, next_cursor=next_cursor, index=status)

    def list_sessions(self, filters: SessionListFilter) -> SessionListPage:
        status = self.status()
        if status.state in {"building", "disabled", "unavailable"}:
            raise AuditReadUnavailableError(status)
        revision = status.revision or 0
        clauses = ["session_key IS NOT NULL", "session_key <> ''"]
        params: list[object] = []
        if filters.query:
            clauses.append("LOWER(session_key) LIKE ?")
            params.append(f"%{filters.query.lower()}%")
        cursor_clause = ""
        if filters.cursor:
            cursor_revision, last_seen, session_key = _decode_cursor(filters.cursor)
            if cursor_revision != revision:
                raise CursorStaleError("audit session cursor revision is stale")
            cursor_clause = (
                "HAVING (MAX(occurred_at) < ? OR "
                "(MAX(occurred_at) = ? AND session_key < ?))"
            )
            params.extend((last_seen, last_seen, session_key))
        sql = f"""
            SELECT session_key, MIN(occurred_at) first_seen, MAX(occurred_at) last_seen,
              COUNT(DISTINCT trace_id) trace_count,
              GROUP_CONCAT(DISTINCT source_type) source_types
            FROM events WHERE {' AND '.join(clauses)} GROUP BY session_key {cursor_clause}
            ORDER BY last_seen DESC, session_key DESC LIMIT ?
        """
        params.append(filters.limit + 1)
        active_ids = self._active_run_ids()
        items: list[SessionListItem] = []
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            for row in rows[: filters.limit]:
                trace_ids = [
                    value[0]
                    for value in connection.execute(
                        """
                        SELECT DISTINCT trace_id FROM events
                        WHERE session_key=? AND trace_id IS NOT NULL
                        """,
                        (row["session_key"],),
                    ).fetchall()
                ]
                placeholders = ",".join("?" for _ in trace_ids)
                trace_rows = connection.execute(
                    """
                    SELECT trace_id, event_type, status, run_id, occurred_at, event_json
                    FROM events WHERE trace_id IN ({placeholders})
                    ORDER BY occurred_at, event_id
                    """.format(placeholders=placeholders),
                    trace_ids,
                ).fetchall()
                by_trace: dict[str, list[sqlite3.Row]] = {}
                for event_row in trace_rows:
                    by_trace.setdefault(event_row["trace_id"], []).append(event_row)
                active_count = warning_count = error_count = 0
                for event_rows in by_trace.values():
                    statuses = {value["status"] for value in event_rows if value["status"]}
                    event_documents = [json.loads(value["event_json"]) for value in event_rows]
                    run_ids = {value["run_id"] for value in event_rows if value["run_id"]}
                    if run_ids & active_ids:
                        active_count += 1
                    if statuses & {"failed", "exhausted"}:
                        error_count += 1
                    elif statuses & {"error", "timeout", "blocked"} or any(
                        document.get("event_type") == "delivery_finished"
                        and document.get("status") == "suppressed"
                        and not expected_delivery_suppression(document, event_documents)
                        for document in event_documents
                    ):
                        warning_count += 1
                latest = max(
                    by_trace,
                    key=lambda trace_id: (by_trace[trace_id][-1]["occurred_at"], trace_id),
                )
                sources = sorted(filter(None, (row["source_types"] or "").split(",")))
                integrity_rows = connection.execute(
                    """
                    SELECT DISTINCT p.status FROM process_integrity p JOIN events e
                      ON e.process_instance_id=p.process_instance_id
                    WHERE e.trace_id IN ({placeholders})
                    """.format(placeholders=placeholders),
                    trace_ids,
                ).fetchall()
                integrity = "valid"
                for candidate in ("invalid", "unknown", "incomplete", "degraded"):
                    if any(value[0] == candidate for value in integrity_rows):
                        integrity = candidate
                        break
                items.append(
                    SessionListItem(
                        session_key=row["session_key"],
                        title=row["session_key"],
                        source_types=sources,
                        first_seen=row["first_seen"],
                        last_seen=row["last_seen"],
                        trace_count=row["trace_count"],
                        active_trace_count=active_count,
                        warning_count=warning_count,
                        error_count=error_count,
                        integrity_status=integrity,
                        latest_trace_id=latest,
                    )
                )
        next_cursor = None
        if len(rows) > filters.limit and items:
            last = items[-1]
            next_cursor = _encode_cursor(
                revision, last.last_seen.isoformat(), last.session_key
            )
        return SessionListPage(items=items, next_cursor=next_cursor, index=status)

    def load_trace_events(self, trace_id: str) -> IndexedTraceEvents:
        status = self.status()
        if status.state in {"building", "disabled", "unavailable"}:
            raise AuditReadUnavailableError(status)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_json FROM events WHERE trace_id=? ORDER BY occurred_at,
                  process_instance_id, durability_epoch, segment_id, segment_sequence, event_id
                """,
                (trace_id,),
            ).fetchall()
            if not rows:
                raise KeyError(trace_id)
            events = [audit_event_adapter.validate_json(row[0]) for row in rows]
            process_ids = sorted({event.process_instance_id for event in events})
            placeholders = ",".join("?" for _ in process_ids)
            integrity_rows = connection.execute(
                f"SELECT * FROM process_integrity WHERE process_instance_id IN ({placeholders})",
                process_ids,
            ).fetchall()
        rank = {"valid": 0, "degraded": 1, "incomplete": 2, "unknown": 3, "invalid": 4}
        integrity_status = max(
            (row["status"] for row in integrity_rows),
            key=lambda value: rank[value],
            default="unknown",
        )
        errors = sorted(
            {code for row in integrity_rows for code in json.loads(row["error_codes_json"])}
        )
        warnings = sorted(
            {code for row in integrity_rows for code in json.loads(row["warning_codes_json"])}
        )
        active = self._active_run_ids()
        return IndexedTraceEvents(
            trace_id=trace_id,
            revision=status.revision or 0,
            events=events,
            integrity_status=integrity_status,
            integrity_error_codes=errors,
            integrity_warning_codes=warnings,
            active_run_ids={event.run_id for event in events if event.run_id in active},
        )

    def load_run_events(self, trace_id: str, run_id: str) -> IndexedTraceEvents:
        result = self.load_trace_events(trace_id)
        selected = [event for event in result.events if event.run_id == run_id]
        if not selected:
            raise KeyError(run_id)
        return result.model_copy(update={"events": selected})

    def list_trace_events(
        self, trace_id: str, *, cursor: str | None = None, limit: int = 200
    ) -> EventPage:
        if not 1 <= limit <= 500:
            raise ValueError("event limit must be between 1 and 500")
        result = self.load_trace_events(trace_id)
        start = 0
        if cursor:
            cursor_revision, _unused, event_id = _decode_cursor(cursor)
            if cursor_revision != result.revision:
                raise CursorStaleError("audit event cursor revision is stale")
            try:
                start = next(
                    index + 1
                    for index, event in enumerate(result.events)
                    if event.event_id == event_id
                )
            except StopIteration as error:
                raise CursorStaleError("audit event cursor target is unavailable") from error
        selected = result.events[start : start + limit]
        next_cursor = None
        if start + limit < len(result.events) and selected:
            next_cursor = _encode_cursor(result.revision, "event", selected[-1].event_id)
        return EventPage(
            events=selected,
            next_cursor=next_cursor,
            revision=result.revision,
            total=len(result.events),
        )

    def load_payload(
        self,
        payload_id: str,
        *,
        audit_root: Path,
        audit_mode: str,
        redactor: AuditRedactor | None = None,
        max_line_bytes: int = 1_048_576,
        max_rendered_chars: int = 200_000,
    ) -> PayloadReadResult:
        if audit_mode == "metadata_only":
            return PayloadReadResult(
                payload_id=payload_id, available=False, reason="metadata_only"
            )
        if audit_mode == "off":
            raise AuditReadUnavailableError(IndexStatus(state="unavailable"))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM payload_locators WHERE payload_id=?", (payload_id,)
            ).fetchone()
        if row is None:
            raise KeyError(payload_id)
        if row["line_length"] > max_line_bytes:
            raise PayloadTooLargeError("payload record exceeds bounded read limit")
        root = audit_root.resolve()
        path = (root / row["path_token"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise PayloadLocatorInvalidError("payload locator escapes audit root") from error
        with path.open("rb") as file:
            file.seek(row["line_offset"])
            line = file.read(row["line_length"])
        if len(line) != row["line_length"] or not line.endswith(b"\n"):
            raise PayloadLocatorInvalidError("payload locator length mismatch")
        try:
            raw = json.loads(line)
            payload = audit_payload_adapter.validate_python(raw)
        except Exception as error:
            raise PayloadLocatorInvalidError("payload locator record invalid") from error
        if (
            payload.payload_id != payload_id
            or payload.event_id != row["event_id"]
            or payload.payload_segment_id != row["segment_id"]
            or payload.payload_segment_sequence != row["segment_sequence"]
            or payload.payload_hash != hash_record(raw, hash_field="payload_hash")
        ):
            raise PayloadLocatorInvalidError("payload locator identity mismatch")
        cleaned, _ = (redactor or AuditRedactor()).redact(
            payload.content.model_dump(mode="json")
        )
        rendered = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
        truncated = len(rendered) > max_rendered_chars
        content: JsonValue = rendered[:max_rendered_chars] if truncated else cleaned
        return PayloadReadResult(
            payload_id=payload_id,
            event_id=payload.event_id,
            payload_kind=payload.payload_kind,
            available=True,
            content=content,
            truncated=truncated,
        )
