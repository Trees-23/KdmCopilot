from __future__ import annotations

import asyncio
import sqlite3
from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest

from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.wiki_adapter import (
    EntryPointWikiAdapter,
    McpWikiAdapter,
    WikiPage,
    WikiRelation,
    capability_digest,
    discover_entry_point_capabilities,
    discover_mcp_capabilities,
)
from nanobot.plugins.memory_wiki import (
    WikiSyncError,
    discover_wiki,
    find_case_duplicates,
    forget_wiki_page,
    sync_wiki_page,
    sync_wiki_relation,
    unlink_wiki_relation,
)


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    apply_migrations(connection)
    connection.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5(object_type UNINDEXED, object_id UNINDEXED, title, summary, body, tags)"
    )
    return connection


def _page(**changes: object) -> WikiPage:
    payload = {
        "page_id": "page-1",
        "title": "Deploy note",
        "content": "Use the staging fixture.",
        "revision_id": "rev-1",
        "page_type": "fact",
        "tags": ["deploy"],
        "updated_at": "2026-09-25T00:00:00+00:00",
    }
    payload.update(changes)
    return WikiPage.from_payload(payload)


def test_capability_discovery_is_source_agnostic_and_digest_stable() -> None:
    points = [
        EntryPoint(name="wiki_read", value="pkg:Read", group="nanobot.tools"),
        EntryPoint(name="wiki_search", value="pkg:Search", group="nanobot.tools"),
        EntryPoint(name="unrelated", value="pkg:Other", group="nanobot.tools"),
    ]
    entry = discover_entry_point_capabilities(points)
    mcp = discover_mcp_capabilities(
        [SimpleNamespace(name="knowledge_read", inputSchema={"type": "object"})]
    )
    assert {item.operation for item in entry} == {"read", "search"}
    assert {item.operation for item in mcp} == {"read"}
    assert capability_digest(entry) == capability_digest(tuple(entry))


class _FakeEntryTool:
    def __init__(self, result):
        self.result = result

    @classmethod
    def create(cls, _ctx):
        return cls({
            "id": "page-1",
            "title": "Deploy note",
            "content": "Use the staging fixture.",
            "revision_id": "rev-1",
        })

    async def execute(self, **_kwargs):
        return self.result


class _FakeProviderPage:
    id = "page-store"
    title = "Provider page"
    content = "Owned by provider"
    page_type = "note"
    tags = ["provider"]
    aliases = []
    updated_at = "2026-09-25T00:00:00+00:00"


class _FakeProviderStore:
    def get_page(self, selector):
        return _FakeProviderPage() if selector == "page-store" else None

    def search(self, query, *, limit):
        return [SimpleNamespace(page=_FakeProviderPage())]

    def upsert_page(self, **_kwargs):
        return _FakeProviderPage()


class StoreTool:
    store = _FakeProviderStore()

    @classmethod
    def create(cls, _ctx):
        return cls()


def test_entry_point_adapter_performs_round_trip_without_provider_storage_access() -> None:
    point = EntryPoint(name="wiki_read", value="tests.memory.test_wiki_phase2:_FakeEntryTool", group="nanobot.tools")
    adapter = EntryPointWikiAdapter("/tmp", points=[point])
    page = asyncio.run(adapter.read("page-1"))
    assert page is not None
    assert page.revision_id == "rev-1"
    assert adapter.capabilities()[0].source == "entrypoint"


def test_entry_point_adapter_uses_provider_store_as_fact_source() -> None:
    point = EntryPoint(name="wiki_read", value="tests.memory.test_wiki_phase2:StoreTool", group="nanobot.tools")
    adapter = EntryPointWikiAdapter("/tmp", points=[point])
    page = asyncio.run(adapter.read("page-store"))
    assert page is not None
    assert page.page_id == "page-store"
    assert page.content_hash.startswith("sha256:")


def test_mcp_adapter_normalizes_structured_page_and_capabilities() -> None:
    calls: list[tuple[str, dict]] = []

    async def call_tool(name: str, arguments: dict):
        calls.append((name, arguments))
        return {
            "page": {"id": "page-1", "title": "Deploy note", "content": "fixture"},
        }

    adapter = McpWikiAdapter(
        call_tool,
        [SimpleNamespace(name="knowledge_read", inputSchema={"type": "object"})],
    )
    page = asyncio.run(adapter.read("page-1"))
    assert page is not None
    assert page.revision_id.startswith("sha256:")
    assert calls == [("knowledge_read", {"selector": "page-1"})]


def test_sync_page_indexes_external_revision_and_case_candidate() -> None:
    connection = _db()
    workspace = "/tmp/phase2-workspace"
    page = _page(
        page_type="case",
        intent="deploy",
        task_signature={"command": "deploy", "variant": "staging"},
        case={"steps": ["verify"], "trace_ids": ["trace-1"], "confidence": 0.8},
    )
    result = sync_wiki_page(connection, workspace=workspace, page=page)
    assert result.changed is True
    assert result.case_id == "case:workspace:page-1"
    row = connection.execute("SELECT current_revision_id,content_hash FROM wiki_pages").fetchone()
    assert tuple(row) == ("rev-1", page.content_hash)
    assert connection.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM memory_fts WHERE object_id='page-1'").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM memory_outbox").fetchone()[0] == 1
    assert find_case_duplicates(connection, page=page) == ["case:workspace:page-1"]

    replay = sync_wiki_page(connection, workspace=workspace, page=page)
    assert replay.changed is False
    assert connection.execute("SELECT count(*) FROM memory_outbox").fetchone()[0] == 1


def test_relation_upsert_is_idempotent_and_unlink_is_rebuildable() -> None:
    connection = _db()
    connection.executemany(
        "INSERT INTO wiki_pages(page_id,namespace,slug,page_type,source_path,content_hash,status,title,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            ("a", "workspace", "a", "fact", "wiki://a", "ha", "candidate", "A", "now", "now"),
            ("b", "workspace", "b", "fact", "wiki://b", "hb", "candidate", "B", "now", "now"),
        ],
    )
    relation = WikiRelation("a", "b", "related", "rev-a")
    first = sync_wiki_relation(connection, workspace="/tmp/phase2", relation=relation)
    second = sync_wiki_relation(connection, workspace="/tmp/phase2", relation=relation)
    assert first == second
    assert connection.execute("SELECT count(*) FROM wiki_relations").fetchone()[0] == 1
    assert unlink_wiki_relation(connection, workspace="/tmp/phase2", relation=relation) == 1
    assert connection.execute("SELECT status FROM wiki_relations").fetchone()[0] == "archived"


def test_forget_tombstones_page_and_prevents_reindex() -> None:
    connection = _db()
    sync_wiki_page(connection, workspace="/tmp/phase2", page=_page())
    assert forget_wiki_page(
        connection,
        workspace="/tmp/phase2",
        page_id="page-1",
        requested_by="test",
    )
    assert connection.execute("SELECT count(*) FROM memory_fts WHERE object_id='page-1'").fetchone()[0] == 0
    with pytest.raises(WikiSyncError, match="tombstoned"):
        sync_wiki_page(connection, workspace="/tmp/phase2", page=_page(revision_id="rev-2"))


def test_sync_rejects_provider_hash_mismatch() -> None:
    connection = _db()
    invalid = WikiPage(
        page_id="invalid",
        title="Invalid",
        content="actual",
        revision_id="rev-1",
        content_hash="sha256:wrong",
    )
    with pytest.raises(WikiSyncError, match="content_hash"):
        sync_wiki_page(connection, workspace="/tmp/phase2", page=invalid)


def test_unavailable_provider_is_explicit() -> None:
    class EmptyAdapter:
        def capabilities(self):
            return ()

    discovery = discover_wiki(EmptyAdapter())
    assert discovery.status == "degraded"
    assert discovery.source == "none"
