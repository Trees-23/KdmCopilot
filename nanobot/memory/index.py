"""SQLite FTS5 capability detection and rebuild helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

FTS_TABLE_SQL = """
CREATE VIRTUAL TABLE {table_name} USING fts5(
  object_type UNINDEXED, object_id UNINDEXED, title, summary, body, tags,
  tokenize='unicode61 remove_diacritics 2'
)
"""


class FtsUnavailableError(RuntimeError):
    """Raised when SQLite was built without FTS5 support."""


@dataclass(frozen=True, slots=True)
class FtsStatus:
    available: bool
    sqlite_version: str
    error: str | None = None


def detect_fts5() -> FtsStatus:
    """Detect FTS5 through a separate temporary SQLite connection."""

    connection = sqlite3.connect(":memory:")
    try:
        version = str(connection.execute("SELECT sqlite_version()").fetchone()[0])
        connection.execute("SELECT fts5(?)", ("probe",))
        connection.execute("CREATE VIRTUAL TABLE fts5_probe USING fts5(value)")
        return FtsStatus(True, version)
    except sqlite3.Error as exc:
        version = str(connection.execute("SELECT sqlite_version()").fetchone()[0])
        return FtsStatus(False, version, str(exc)[:500])
    finally:
        connection.close()


def initialize_fts(connection: sqlite3.Connection) -> FtsStatus:
    """Create the FTS table when supported; otherwise return degraded status."""

    status = detect_fts5()
    if not status.available:
        return status
    connection.execute(FTS_TABLE_SQL.format(table_name="memory_fts"))
    connection.commit()
    return status


def _active_memory_rows(connection: sqlite3.Connection) -> list[dict[str, str]]:
    rows = connection.execute(
        "SELECT r.memory_id,r.title,r.summary,r.tags_json,v.content "
        "FROM memory_records r JOIN memory_revisions v ON v.revision_id=r.current_revision_id "
        "WHERE r.status='active' AND NOT EXISTS (SELECT 1 FROM tombstones t "
        "WHERE t.object_type='memory' AND t.object_id=r.memory_id)"
    )
    return [
        {
            "object_type": "memory",
            "object_id": row[0],
            "title": row[1],
            "summary": row[2],
            "body": row[4],
            "tags": row[3],
        }
        for row in rows
    ]


def rebuild_fts(
    connection: sqlite3.Connection,
    rows: list[dict[str, str]] | None = None,
) -> int:
    """Atomically rebuild FTS rows, retaining the previous index on failure."""

    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone():
        raise FtsUnavailableError("memory_fts has not been initialized")
    source_rows = _active_memory_rows(connection) if rows is None else rows
    filtered: list[dict[str, str]] = []
    for row in source_rows:
        if row.get("object_type") not in {"memory", "wiki_page", "case", "skill", "trace_summary"}:
            raise ValueError("invalid FTS object type")
        tombstone = connection.execute(
            "SELECT 1 FROM tombstones WHERE object_type=? AND object_id=?",
            (row["object_type"], row["object_id"]),
        ).fetchone()
        if tombstone:
            continue
        filtered.append({key: str(row.get(key, "")) for key in ("object_type", "object_id", "title", "summary", "body", "tags")})

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(FTS_TABLE_SQL.format(table_name="memory_fts_rebuild"))
        connection.executemany(
            "INSERT INTO memory_fts_rebuild(object_type,object_id,title,summary,body,tags) "
            "VALUES(?,?,?,?,?,?)",
            [
                (r["object_type"], r["object_id"], r["title"], r["summary"], r["body"], r["tags"])
                for r in filtered
            ],
        )
        count = connection.execute("SELECT count(*) FROM memory_fts_rebuild").fetchone()[0]
        if count != len(filtered):
            raise RuntimeError("FTS rebuild row count mismatch")
        connection.execute("DROP TABLE memory_fts")
        connection.execute("ALTER TABLE memory_fts_rebuild RENAME TO memory_fts")
        connection.commit()
        return count
    except Exception:
        connection.rollback()
        connection.execute("DROP TABLE IF EXISTS memory_fts_rebuild")
        connection.commit()
        raise


def search_memory(connection: sqlite3.Connection, query: str, *, limit: int = 20) -> list[sqlite3.Row]:
    """Search active memory records while excluding archived and tombstoned rows."""

    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    return list(
        connection.execute(
            "SELECT f.object_id,f.title,f.summary,bm25(memory_fts) AS score "
            "FROM memory_fts f JOIN memory_records r ON f.object_type='memory' "
            "AND f.object_id=r.memory_id WHERE f.object_type='memory' "
            "AND memory_fts MATCH ? AND r.status='active' AND NOT EXISTS "
            "(SELECT 1 FROM tombstones t WHERE t.object_type=f.object_type AND t.object_id=f.object_id) "
            "ORDER BY score LIMIT ?",
            (query, limit),
        )
    )
