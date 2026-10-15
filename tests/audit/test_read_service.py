import json

import pytest

from nanobot.audit.index import AuditIndex, AuditIndexer
from nanobot.audit.read_service import (
    AuditReadService,
    CursorStaleError,
    IndexStatus,
    PayloadLocatorInvalidError,
    PayloadTooLargeError,
    SessionListFilter,
    TraceListFilter,
    expected_delivery_suppression,
)
from nanobot.audit.schema import TurnInputPayloadDraft
from nanobot.audit.writer import AuditWriter, CommitItem
from tests.audit.test_writer import _event, _item


@pytest.mark.parametrize("filters", [
    TraceListFilter(session_key="wanted-session"),
    TraceListFilter(source_type="websocket"),
    TraceListFilter(model="wanted-model"),
    TraceListFilter(tool="grep"),
    TraceListFilter(session_key="wanted-session", source_type="websocket", model="wanted-model", tool="grep"),
])
def test_trace_filters_bound_query_work_and_keep_events_from_the_entire_trace(tmp_path, filters):
    index = AuditIndex.open(tmp_path / "audit-index.sqlite")
    rows = []
    for number in range(10_002):
        wanted = number >= 10_000
        last = number == 10_001
        rows.append((
            f"event-{number}", "trace_created", "2026-10-16T00:00:00+00:00",
            "wanted-trace" if wanted else "large-unrelated-trace",
            "other-session" if last else "wanted-session" if wanted else "noise-session",
            "websocket" if last else "cli", "wanted-model" if last else "other-model",
            "grep" if last else "read_file", "process", "segment", number + 1, 1,
            json.dumps({"event_type": "trace_created"}),
        ))
    index.connection.executemany(
        "INSERT INTO events(event_id,event_type,occurred_at,trace_id,session_key,source_type,model,tool_name,"
        "process_instance_id,segment_id,segment_sequence,durability_epoch,event_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    index.connection.commit()
    index.close()

    class BoundedReadService(AuditReadService):
        def _connect(self):
            connection = super()._connect()
            steps = 0

            def bound_sqlite_work():
                nonlocal steps
                steps += 1000
                return int(steps > 1_000_000)

            connection.set_progress_handler(bound_sqlite_work, 1000)
            return connection

    service = BoundedReadService(
        tmp_path / "audit-index.sqlite", status_provider=lambda: IndexStatus(state="ready", revision=1),
    )
    page = service.list_traces(filters)
    assert [(row.trace_id, row.event_count) for row in page.items] == [("wanted-trace", 2)]


def test_legacy_webui_suppression_requires_websocket_source_and_successful_run() -> None:
    delivery = {
        "event_type": "delivery_finished",
        "status": "suppressed",
        "run_id": "run-1",
        "suppression_reason": None,
    }
    successful_webui = [
        {"event_type": "turn_started", "source_type": "websocket", "run_id": "run-1"},
        {"event_type": "run_finished", "status": "succeeded", "run_id": "run-1"},
        delivery,
    ]
    failed_webui = [
        successful_webui[0],
        {"event_type": "run_finished", "status": "failed", "run_id": "run-1"},
        delivery,
    ]
    assert expected_delivery_suppression(delivery, successful_webui) is True
    assert expected_delivery_suppression(delivery, failed_webui) is False


async def test_indexed_read_service_lists_and_loads_typed_events(tmp_path) -> None:
    writer = AuditWriter(tmp_path, fsync_interval_seconds=0.01)
    await writer.start()
    await writer.submit(_item(1, payload=False))
    await writer.close()
    indexer = AuditIndexer(tmp_path)
    indexer.update()
    service = AuditReadService(indexer.index_path)

    page = service.list_traces(TraceListFilter())
    detail = service.load_trace_events("t1")
    sessions = service.list_sessions(SessionListFilter())

    assert page.index.state == "ready"
    assert [item.trace_id for item in page.items] == ["t1"]
    assert [event.event_id for event in detail.events] == ["e1"]
    assert sessions.items[0].session_key == "cli:direct"
    assert sessions.items[0].trace_count == 1


async def test_cursor_is_bound_to_index_revision(tmp_path) -> None:
    writer = AuditWriter(tmp_path, fsync_interval_seconds=0.01)
    await writer.start()
    await writer.submit(_item(1, payload=False))
    await writer.submit(_item(2, payload=False))
    await writer.close()
    indexer = AuditIndexer(tmp_path)
    indexer.update()
    service = AuditReadService(indexer.index_path)
    first = service.list_traces(TraceListFilter(limit=1))
    assert first.next_cursor is not None

    second_writer = AuditWriter(tmp_path, fsync_interval_seconds=0.01)
    await second_writer.start()
    await second_writer.submit(_item(3, payload=False))
    await second_writer.close()
    indexer.update()
    with pytest.raises(CursorStaleError):
        service.list_traces(TraceListFilter(limit=1, cursor=first.next_cursor))


async def test_noop_index_refresh_preserves_revision_for_etag_stability(tmp_path) -> None:
    writer = AuditWriter(tmp_path, fsync_interval_seconds=0.01)
    await writer.start()
    await writer.submit(_item(1, payload=False))
    await writer.close()
    indexer = AuditIndexer(tmp_path)
    indexer.update()
    service = AuditReadService(indexer.index_path)
    first_revision = service.status().revision

    indexer.update()

    assert service.status().revision == first_revision


async def test_payload_point_read_redacts_and_enforces_locator_bounds(tmp_path) -> None:
    writer = AuditWriter(tmp_path, fsync_interval_seconds=0.01)
    await writer.start()
    payload = TurnInputPayloadDraft(
        payload_id="secret-payload",
        event_id="e1",
        content={
            "role": "user",
            "content": {"Authorization": "Bearer test-secret", "safe": "visible"},
            "media_refs": [],
            "source_message_id": None,
        },
    )
    await writer.submit(
        CommitItem(event=_event(1), payload=payload, estimated_bytes=512, critical=True)
    )
    await writer.close()
    indexer = AuditIndexer(tmp_path)
    indexer.update()
    service = AuditReadService(indexer.index_path)

    result = service.load_payload(
        "secret-payload", audit_root=tmp_path, audit_mode="full"
    )
    assert result.available is True
    assert "test-secret" not in str(result.content)
    assert "[REDACTED:CREDENTIAL]" in str(result.content)

    index = AuditIndex.open(indexer.index_path)
    index.connection.execute(
        "UPDATE payload_locators SET line_length=? WHERE payload_id=?",
        (1_048_577, "secret-payload"),
    )
    index.connection.commit()
    index.close()
    with pytest.raises(PayloadTooLargeError):
        service.load_payload("secret-payload", audit_root=tmp_path, audit_mode="full")

    index = AuditIndex.open(indexer.index_path)
    index.connection.execute(
        "UPDATE payload_locators SET line_length=1, path_token='../outside' WHERE payload_id=?",
        ("secret-payload",),
    )
    index.connection.commit()
    index.close()
    with pytest.raises(PayloadLocatorInvalidError):
        service.load_payload("secret-payload", audit_root=tmp_path, audit_mode="full")
