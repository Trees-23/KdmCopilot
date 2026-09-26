from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import nanobot.memory.trace_indexer as trace_indexer
from nanobot.audit.reader import AuditReader
from nanobot.audit.redaction import AuditRedactor, RedactionError
from nanobot.audit.writer import AuditWriter
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.retrieval_events import (
    RetrievalEvent,
    record_retrieval,
    retrieval_failure_event,
    search_trace_summaries,
)
from nanobot.memory.trace_indexer import (
    PAYLOAD_RETENTION_DAYS,
    TRACE_RETENTION_DAYS,
    AuditTrace,
    index_audit_root,
    index_trace_summary,
    iter_audit_traces,
    rebuild_trace_index,
    summarize_trace,
    update_trace_index,
)
from tests.audit.test_writer import _item


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    apply_migrations(connection)
    return connection


def _event(event_id: str, occurred_at: datetime, event_type: str = "tool_finished", **kwargs: object):
    return SimpleNamespace(
        event_id=event_id,
        occurred_at=occurred_at,
        event_type=event_type,
        run_id=kwargs.pop("run_id", "run-1"),
        parent_run_id=kwargs.pop("parent_run_id", None),
        session_key=kwargs.pop("session_key", "session-1"),
        tool_name=kwargs.pop("tool_name", "read_file"),
        error_summary=kwargs.pop("error_summary", None),
        **kwargs,
    )


def test_summary_is_deterministic_and_does_not_copy_payload() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    trace = AuditTrace(
        "trace-1",
        "process-1",
        (
            _event("e1", started, event_type="run_started"),
            _event("e2", started + timedelta(seconds=2), error_summary="Bearer sk-secret-value"),
            _event("e3", started + timedelta(seconds=3), event_type="run_finished", status="failed"),
        ),
        (),
        "/tmp/audit",
        4,
    )
    summary = summarize_trace(trace, workspace="/tmp/workspace")
    assert "sk-secret-value" not in summary.summary
    assert "payload" not in summary.summary
    assert summary.outcome == "failed"
    assert summary.payload_expire_at == (started + timedelta(days=PAYLOAD_RETENTION_DAYS)).isoformat()
    assert summary.trace_expire_at == (started + timedelta(days=TRACE_RETENTION_DAYS)).isoformat()


def test_trace_index_upsert_is_idempotent() -> None:
    connection = _db()
    started = datetime(2026, 1, 1, tzinfo=UTC)
    trace = AuditTrace("trace-1", "p1", (_event("e1", started),), (), "/tmp/audit", 1)
    summary = summarize_trace(trace, workspace="/tmp/workspace")
    assert index_trace_summary(connection, summary) is True
    connection.commit()
    assert index_trace_summary(connection, summary) is False
    assert connection.execute("SELECT count(*) FROM trace_index").fetchone()[0] == 1


def test_summary_cursor_changes_when_committed_prefix_advances() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    first = summarize_trace(
        AuditTrace("trace-1", "p1", (_event("e1", started),), (), "/tmp/audit", 1),
        workspace="/tmp/workspace",
    )
    second = summarize_trace(
        AuditTrace(
            "trace-1", "p1", (_event("e1", started), _event("e2", started + timedelta(seconds=1))),
            (), "/tmp/audit", 2,
        ),
        workspace="/tmp/workspace",
    )
    assert first.event_cursor != second.event_cursor
    assert first.summary_hash != second.summary_hash


async def _committed_audit_root(tmp_path) -> tuple[object, str]:
    audit_root = tmp_path / "audit"
    writer = AuditWriter(audit_root, fsync_interval_seconds=0.01)
    await writer.start()
    await writer.submit(_item(1, payload=True))
    process_id = writer.process_id
    await writer.close()
    return audit_root, process_id


async def test_audit_reader_adapter_indexes_only_committed_prefix(tmp_path) -> None:
    audit_root, process_id = await _committed_audit_root(tmp_path)
    result = AuditReader(audit_root).read_process(process_id)
    event_path = next((audit_root / "events").rglob("*.jsonl"))
    event_path.write_text(event_path.read_text() + event_path.read_text())

    traces = list(iter_audit_traces(audit_root))

    assert len(result.events) == 1
    assert len(traces) == 1
    assert traces[0].events[0].event_id == "e1"
    assert traces[0].payloads[0].payload_id == "d1"


async def test_full_and_incremental_replay_are_idempotent_and_payload_free(tmp_path) -> None:
    audit_root, _ = await _committed_audit_root(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = _db()

    assert rebuild_trace_index(audit_root, workspace, connection=connection) == 1
    assert update_trace_index(audit_root, workspace, connection=connection) == 0
    row = connection.execute("SELECT summary,event_cursor FROM trace_index").fetchone()

    assert "message 1" not in row[0]
    assert row[1]


def test_trace_query_returns_structured_active_rows_and_hides_degraded_or_expired() -> None:
    connection = _db()
    now = datetime(2026, 1, 20, tzinfo=UTC)
    connection.executemany(
        """INSERT INTO trace_index
        (trace_id,workspace,started_at,outcome,event_count,tool_count,summary,index_status,redaction_version,
         trace_expire_at,source_path,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("trace-visible", "/workspace", now.isoformat(), "succeeded", 3, 1, "safe", "indexed",
             "audit-redactor-v1", (now + timedelta(days=1)).isoformat(), "/audit", now.isoformat(), now.isoformat()),
            ("trace-degraded", "/workspace", now.isoformat(), "failed", 1, 0, "", "degraded",
             "audit-redactor-v1", (now + timedelta(days=1)).isoformat(), "/audit", now.isoformat(), now.isoformat()),
            ("trace-expired", "/workspace", now.isoformat(), "failed", 1, 0, "", "indexed",
             "audit-redactor-v1", (now - timedelta(seconds=1)).isoformat(), "/audit", now.isoformat(), now.isoformat()),
        ],
    )

    results = search_trace_summaries(connection, workspace="/workspace", now=now)

    assert [result.trace_id for result in results] == ["trace-visible"]
    assert results[0].summary == "safe"


def test_retrieval_events_are_append_only_and_failures_are_visible() -> None:
    connection = _db()
    event = RetrievalEvent(
        session_key="session-1",
        intent="trace_query",
        scopes=["trace"],
        result_ids=["trace-1:revision-1"],
        injected_ids=["trace-1"],
        injected_context={"ids": ["trace-1"], "overlay": "metadata"},
        outcome="succeeded",
        query="do not persist this query",
        retrieval_id="retrieval-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert record_retrieval(connection, event) == "retrieval-1"
    failure = retrieval_failure_event(
        session_key="session-1", intent="history_query", error_code="index_degraded"
    )
    record_retrieval(connection, failure)

    rows = connection.execute(
        "SELECT intent,query_digest,outcome,error_code FROM retrieval_events ORDER BY created_at"
    ).fetchall()
    assert len(rows) == 2
    assert "do not persist this query" not in rows[0][1]
    assert tuple(rows[1][2:]) == ("failed", "index_degraded")


class _FailingRedactor(AuditRedactor):
    def redact(self, value):  # type: ignore[no-untyped-def]
        raise RedactionError("simulated failure")


async def test_redaction_failure_keeps_audit_and_queues_retry(tmp_path) -> None:
    audit_root, process_id = await _committed_audit_root(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = _db()
    before = [path.read_bytes() for path in audit_root.rglob("*.jsonl")]

    assert index_audit_root(audit_root, workspace, connection=connection, redactor=_FailingRedactor()) == 0

    assert [path.read_bytes() for path in audit_root.rglob("*.jsonl")] == before
    degraded = connection.execute(
        "SELECT index_status,last_error FROM trace_index"
    ).fetchone()
    retry = connection.execute(
        "SELECT object_type,object_id,status,payload_json FROM memory_outbox"
    ).fetchone()
    assert degraded[0] == "degraded"
    assert degraded[1] == "redaction_failed"
    assert retry[0] == "trace_summary"
    assert retry[2] == "pending"
    assert process_id not in retry[3]


async def test_index_failure_keeps_audit_and_queues_retry(tmp_path, monkeypatch) -> None:
    audit_root, _ = await _committed_audit_root(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = _db()
    before = [path.read_bytes() for path in audit_root.rglob("*.jsonl")]

    def fail_index(*_args, **_kwargs) -> bool:
        raise RuntimeError("simulated index failure")

    monkeypatch.setattr(trace_indexer, "index_trace_summary", fail_index)
    assert index_audit_root(audit_root, workspace, connection=connection) == 0

    assert [path.read_bytes() for path in audit_root.rglob("*.jsonl")] == before
    assert connection.execute("SELECT last_error FROM trace_index").fetchone()[0] == "index_failed"
    assert connection.execute("SELECT status FROM memory_outbox").fetchone()[0] == "pending"
