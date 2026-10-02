"""Synchronize provider-owned Wiki revisions into memory indexes.

The functions in this module never write Wiki Markdown.  An adapter performs
that operation in the provider first; this module records only revision IDs,
hashes, metadata, relations, and rebuildable FTS rows in the memory database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from nanobot.memory.outbox import enqueue_outbox
from nanobot.memory.wiki_adapter import (
    WikiAdapter,
    WikiCapability,
    WikiPage,
    WikiRelation,
    capability_digest,
)


class WikiSyncError(RuntimeError):
    """Raised when a provider revision cannot be safely indexed."""


@dataclass(frozen=True, slots=True)
class WikiSyncResult:
    page_id: str
    revision_id: str
    content_hash: str
    changed: bool
    index_status: str
    outbox_id: str | None = None
    case_id: str | None = None


@dataclass(frozen=True, slots=True)
class WikiDiscovery:
    source: str
    version: str | None
    capabilities: tuple[WikiCapability, ...]
    schema_digest: str
    status: str


def _now(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _page_type(value: str) -> str:
    aliases = {"note": "fact", "knowledge": "fact", "case": "case"}
    normalized = aliases.get(value.strip().lower(), value.strip().lower())
    allowed = {"fact", "decision", "case", "skill_note", "index"}
    if normalized not in allowed:
        raise WikiSyncError(f"unsupported Wiki page type: {value}")
    return normalized


def _summary(page: WikiPage) -> str:
    if page.summary.strip():
        return page.summary.strip()[:2000]
    return " ".join(page.content.split())[:2000]


def _tombstoned(connection: sqlite3.Connection, page_id: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM tombstones WHERE object_type='wiki_page' AND object_id=?",
            (page_id,),
        ).fetchone()
    )


def _fts_available(connection: sqlite3.Connection) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()
    )


def _replace_fts_row(
    connection: sqlite3.Connection,
    *,
    object_type: str,
    object_id: str,
    title: str,
    summary: str,
    body: str,
    tags: str,
) -> bool:
    if not _fts_available(connection):
        return False
    connection.execute(
        "DELETE FROM memory_fts WHERE object_type=? AND object_id=?", (object_type, object_id)
    )
    connection.execute(
        "INSERT INTO memory_fts(object_type,object_id,title,summary,body,tags) VALUES(?,?,?,?,?,?)",
        (object_type, object_id, title, summary, body, tags),
    )
    return True


def discover_wiki(adapter: WikiAdapter) -> WikiDiscovery:
    """Capture provider capability metadata without changing local state."""

    try:
        capabilities = adapter.capabilities()
    except Exception:
        capabilities = ()
    if not capabilities:
        return WikiDiscovery(
            source="none",
            version=None,
            capabilities=(),
            schema_digest=capability_digest(()),
            status="degraded",
        )
    sources = {item.source for item in capabilities}
    source = "+".join(sorted(sources))
    versions = sorted({item.version for item in capabilities if item.version})
    return WikiDiscovery(
        source=source,
        version=versions[0] if len(versions) == 1 else ("mixed" if versions else None),
        capabilities=capabilities,
        schema_digest=capability_digest(capabilities),
        status="ready",
    )


def sync_wiki_page(
    connection: sqlite3.Connection,
    *,
    workspace: str | Path,
    page: WikiPage,
    namespace: str = "workspace",
    now: datetime | None = None,
) -> WikiSyncResult:
    """Index one provider revision, preserving external identity and hash."""

    expected_hash = "sha256:" + hashlib.sha256(
        json.dumps(page.content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if page.content_hash != expected_hash:
        raise WikiSyncError("Wiki page content_hash does not match content")
    if _tombstoned(connection, page.page_id):
        raise WikiSyncError(f"Wiki page is tombstoned: {page.page_id}")
    page_type = _page_type(page.page_type)
    timestamp = _now(now)
    source_path = page.source_path or f"wiki://{page.page_id}"
    existing = connection.execute(
        "SELECT page_id,current_revision_id,content_hash,created_at FROM wiki_pages "
        "WHERE namespace=? AND slug=?",
        (namespace, page.page_id),
    ).fetchone()
    changed = not existing or existing[1] != page.revision_id or existing[2] != page.content_hash
    if not changed:
        return WikiSyncResult(
            page_id=page.page_id,
            revision_id=page.revision_id,
            content_hash=page.content_hash,
            changed=False,
            index_status="indexed",
        )

    case_id: str | None = None
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """INSERT INTO wiki_pages
            (page_id,namespace,slug,page_type,source_path,content_hash,current_revision_id,status,
             sensitivity,title,summary,tags_json,source_refs_json,created_at,updated_at,archived_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            ON CONFLICT(namespace,slug) DO UPDATE SET page_type=excluded.page_type,
              source_path=excluded.source_path,content_hash=excluded.content_hash,
              current_revision_id=excluded.current_revision_id,status='candidate',
              title=excluded.title,summary=excluded.summary,tags_json=excluded.tags_json,
              source_refs_json=excluded.source_refs_json,updated_at=excluded.updated_at,
              archived_at=NULL""",
            (
                page.page_id,
                namespace,
                page.page_id,
                page_type,
                source_path,
                page.content_hash,
                page.revision_id,
                "candidate",
                "private",
                page.title,
                _summary(page),
                _json(list(page.tags)),
                _json([page.source_path] if page.source_path else []),
                existing[3] if existing else timestamp,
                page.updated_at or timestamp,
            ),
        )
        tags = _json(list(page.tags))
        index_status = "indexed" if _replace_fts_row(
            connection,
            object_type="case" if page_type == "case" else "wiki_page",
            object_id=page.page_id,
            title=page.title,
            summary=_summary(page),
            body=page.content,
            tags=tags,
        ) else "degraded"
        if page_type == "case":
            case_id = f"case:{namespace}:{page.page_id}"
            case_data = page.case_data
            task_signature = page.task_signature or case_data.get("task_signature", {})
            outcome = case_data.get("outcome", {"summary": _summary(page)})
            connection.execute(
                """INSERT INTO cases
                (case_id,page_id,skill_id,skill_version,intent,task_signature_json,
                 preconditions_json,steps_json,outcome_json,failure_patterns_json,trace_ids_json,
                 confidence,user_confirmed,status,revision_id,previous_revision_id,created_at,updated_at,
                 last_used_at,use_count)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(page_id) DO UPDATE SET intent=excluded.intent,
                  task_signature_json=excluded.task_signature_json,
                  preconditions_json=excluded.preconditions_json,steps_json=excluded.steps_json,
                  outcome_json=excluded.outcome_json,failure_patterns_json=excluded.failure_patterns_json,
                  trace_ids_json=excluded.trace_ids_json,confidence=excluded.confidence,
                  status='candidate',previous_revision_id=cases.revision_id,
                  revision_id=excluded.revision_id,updated_at=excluded.updated_at""",
                (
                    case_id,
                    page.page_id,
                    case_data.get("skill_id"),
                    case_data.get("skill_version"),
                    page.intent or str(case_data.get("intent", "")),
                    _json(task_signature),
                    _json(case_data.get("preconditions", [])),
                    _json(case_data.get("steps", [])),
                    _json(outcome),
                    _json(case_data.get("failure_patterns", [])),
                    _json(case_data.get("trace_ids", [])),
                    float(case_data.get("confidence", 0)),
                    0,
                    "candidate",
                    page.revision_id,
                    None,
                    existing[3] if existing else timestamp,
                    page.updated_at or timestamp,
                    None,
                    0,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    outbox_id = enqueue_outbox(
        connection,
        workspace=str(Path(workspace).resolve()),
        object_type="case" if page_type == "case" else "wiki_page",
        object_id=case_id or page.page_id,
        content_hash=page.content_hash,
        revision_id=page.revision_id,
        payload={"page_id": page.page_id, "revision_id": page.revision_id},
    )
    return WikiSyncResult(
        page_id=page.page_id,
        revision_id=page.revision_id,
        content_hash=page.content_hash,
        changed=True,
        index_status=index_status,
        outbox_id=outbox_id,
        case_id=case_id,
    )


def sync_wiki_relation(
    connection: sqlite3.Connection,
    *,
    workspace: str | Path,
    relation: WikiRelation,
    now: datetime | None = None,
) -> str:
    """Insert a typed relation idempotently and enqueue its source revision."""

    relation_type = relation.relation_type.strip() or "related"
    relation_id = _hash(f"{relation.from_page_id}\0{relation.to_page_id}\0{relation_type}")
    timestamp = _now(now)
    connection.execute(
        """INSERT INTO wiki_relations
        (relation_id,from_page_id,to_page_id,relation_type,weight,status,source_revision_id,created_at,updated_at)
        VALUES(?,?,?,?,1,'active',?,?,?) ON CONFLICT(from_page_id,to_page_id,relation_type)
        DO UPDATE SET status='active',source_revision_id=excluded.source_revision_id,
          updated_at=excluded.updated_at""",
        (
            relation_id,
            relation.from_page_id,
            relation.to_page_id,
            relation_type,
            relation.revision_id,
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    enqueue_outbox(
        connection,
        workspace=str(Path(workspace).resolve()),
        object_type="wiki_page",
        object_id=relation.from_page_id,
        content_hash=_hash(relation_id),
        operation="upsert",
        revision_id=relation.revision_id,
        payload={"relation_id": relation_id, "to_page_id": relation.to_page_id},
    )
    return relation_id


def unlink_wiki_relation(
    connection: sqlite3.Connection,
    *,
    workspace: str | Path,
    relation: WikiRelation,
    now: datetime | None = None,
) -> int:
    """Soft-delete a relation without deleting provider evidence."""

    cursor = connection.execute(
        "UPDATE wiki_relations SET status='archived',updated_at=? WHERE from_page_id=? "
        "AND to_page_id=? AND relation_type=? AND status='active'",
        (_now(now), relation.from_page_id, relation.to_page_id, relation.relation_type),
    )
    connection.commit()
    if cursor.rowcount:
        enqueue_outbox(
            connection,
            workspace=str(Path(workspace).resolve()),
            object_type="wiki_page",
            object_id=relation.from_page_id,
            content_hash=_hash(
                f"unlink\0{relation.from_page_id}\0{relation.to_page_id}\0{relation.relation_type}"
            ),
            operation="archive",
            revision_id=relation.revision_id,
            payload={"to_page_id": relation.to_page_id, "relation": relation.relation_type},
        )
    return cursor.rowcount


def find_case_duplicates(
    connection: sqlite3.Connection,
    *,
    page: WikiPage,
    namespace: str = "workspace",
) -> list[str]:
    """Find existing candidates by content hash or normalized task signature."""

    task_signature = _json(page.task_signature)
    rows = connection.execute(
        """SELECT c.case_id FROM cases c JOIN wiki_pages p ON p.page_id=c.page_id
        WHERE p.namespace=? AND (p.content_hash=? OR c.task_signature_json=?)
        ORDER BY c.updated_at DESC""",
        (namespace, page.content_hash, task_signature),
    )
    return [str(row[0]) for row in rows]


def forget_wiki_page(
    connection: sqlite3.Connection,
    *,
    workspace: str | Path,
    page_id: str,
    requested_by: str,
    reason_code: str = "user_forget",
    now: datetime | None = None,
) -> bool:
    """Tombstone a page and remove only its rebuildable local index rows."""

    row = connection.execute(
        "SELECT content_hash,current_revision_id FROM wiki_pages WHERE page_id=?", (page_id,)
    ).fetchone()
    if row is None:
        return False
    timestamp = _now(now)
    connection.execute(
        """INSERT INTO tombstones
        (tombstone_id,object_type,object_id,content_hash,reason_code,requested_by,deleted_at,expires_at,source_revision_id)
        VALUES(?,?,?,?,?,?,?,NULL,?) ON CONFLICT(object_type,object_id) DO NOTHING""",
        (
            _hash(f"tombstone\0wiki_page\0{page_id}"),
            "wiki_page",
            page_id,
            row[0],
            reason_code,
            requested_by,
            timestamp,
            row[1],
        ),
    )
    connection.execute(
        "UPDATE wiki_pages SET status='archived',archived_at=?,updated_at=? WHERE page_id=?",
        (timestamp, timestamp, page_id),
    )
    if _fts_available(connection):
        connection.execute("DELETE FROM memory_fts WHERE object_id=?", (page_id,))
    connection.commit()
    enqueue_outbox(
        connection,
        workspace=str(Path(workspace).resolve()),
        object_type="wiki_page",
        object_id=page_id,
        content_hash=row[0],
        operation="tombstone",
        revision_id=row[1],
        payload={"reason_code": reason_code},
    )
    return True


def sync_provider_page(
    connection: sqlite3.Connection,
    *,
    workspace: str | Path,
    adapter: WikiAdapter,
    page: Mapping[str, Any],
    namespace: str = "workspace",
) -> WikiSyncResult:
    """Write a provider revision first, then index the returned immutable revision."""

    # The adapter owns the external write and returns the authoritative revision.
    import asyncio

    async def write() -> WikiPage:
        return await adapter.upsert(page)

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        raise WikiSyncError("sync_provider_page must be called outside a running event loop")
    written = asyncio.run(write())
    return sync_wiki_page(connection, workspace=workspace, page=written, namespace=namespace)


__all__ = [
    "WikiDiscovery",
    "WikiSyncError",
    "WikiSyncResult",
    "discover_wiki",
    "find_case_duplicates",
    "forget_wiki_page",
    "sync_provider_page",
    "sync_wiki_page",
    "sync_wiki_relation",
    "unlink_wiki_relation",
]
