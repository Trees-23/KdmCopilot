"""In-process asset retrieval; old case/wiki tables are not an asset data source."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from nanobot.memory.asset_render import ASSET_TOKEN_BUDGET, fit_asset_budget, render_asset_body
from nanobot.memory.db import connect_memory_db
from nanobot.memory.intent import IntentDecision, IntentRouter
from nanobot.memory.policy import MemoryScopePolicy
from nanobot.memory.retrieval_events import RetrievalEvent, record_retrieval
from nanobot.memory.retriever import MemoryRetriever, _match_score, _matches_query


@dataclass(frozen=True, slots=True)
class AssetHit:
    asset_id: str
    asset_level: str
    title: str
    body: str
    score: float
    source_group: str | None


class AssetRetriever(Protocol):
    def retrieve(
        self, query: str, *, workspace: str,
        asset_levels: tuple[str, ...] = ("L2", "L1"), limit: int = 8,
    ) -> tuple[AssetHit, ...]: ...


@dataclass(frozen=True, slots=True)
class AssetRetrievalResult:
    hits: tuple[AssetHit, ...] = ()
    text: str = ""
    tokens: int = 0
    outcome: str = "empty"
    error_code: str | None = None
    digest: str = ""
    retrieval_id: str | None = None

    @property
    def injected_ids(self) -> tuple[str, ...]:
        return tuple(hit.asset_id for hit in self.hits)


class SQLiteAssetRetriever:
    def retrieve(
        self, query: str, *, workspace: str,
        asset_levels: tuple[str, ...] = ("L2", "L1"), limit: int = 8,
    ) -> tuple[AssetHit, ...]:
        if not 1 <= limit <= 100 or set(asset_levels) - {"L1", "L2"}:
            raise ValueError("invalid asset retrieval bounds")
        if not asset_levels:
            return ()
        root = Path(workspace).expanduser().resolve()
        db_path = root / ".nanobot" / "memory.sqlite3"
        if not db_path.is_file():
            raise FileNotFoundError("asset_db_unavailable")
        connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT * FROM memory_assets WHERE workspace=? AND status='active' AND visibility='shared' "
                "AND asset_level IN (" + ",".join("?" for _ in asset_levels) + ")",
                (str(root), *asset_levels),
            )
            now = datetime.now(UTC)
            hits = []
            for source in rows:
                row = dict(source)
                if not _matches_query(query, str(row["task_goal"] or ""), row["search_text"]):
                    continue
                title, body = render_asset_body(row)
                score = _match_score(query, title, row["search_text"], 0.2)
                score += 0.15 * MemoryRetriever._freshness(row["updated_at"], now)
                hits.append(AssetHit(row["asset_id"], row["asset_level"], title, body, score, row["source_group"]))
            return tuple(sorted(hits, key=lambda hit: (0 if hit.asset_level == "L2" else 1, -hit.score, hit.asset_id))[:limit])
        finally:
            connection.close()


def retrieve_asset_context(
    query: str, *, workspace: str | Path, session_key: str, trace_id: str | None,
    turn_id: str | None, token_budget: int = ASSET_TOKEN_BUDGET,
    decision: IntentDecision | None = None, retriever: AssetRetriever | None = None,
) -> AssetRetrievalResult:
    decision = decision or IntentRouter().classify(query)
    scope = MemoryScopePolicy().for_intent(decision)
    if decision.intent in {"forget", "memory_write"} or not scope.allows("asset"):
        return AssetRetrievalResult(outcome="skipped")
    root = Path(workspace).expanduser().resolve()
    selected, text, tokens, outcome, error_code = (), "", 0, "empty", None
    try:
        hits = (retriever or SQLiteAssetRetriever()).retrieve(query, workspace=str(root))
        rendered = fit_asset_budget(hits, token_budget)
        selected, text, tokens = rendered.hits, rendered.text, rendered.tokens
        outcome = "indexed" if selected else "empty"
    except FileNotFoundError:
        outcome, error_code = "degraded", "asset_db_unavailable"
    except (OSError, sqlite3.Error, TypeError, ValueError):
        outcome, error_code = "degraded", "asset_query_failed"
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    retrieval_id = None
    if (root / ".nanobot" / "memory.sqlite3").is_file():
        connection = None
        try:
            connection = connect_memory_db(root)
            retrieval_id = record_retrieval(connection, RetrievalEvent(
                session_key=session_key, intent=decision.intent, scopes=["asset"],
                result_ids=[hit.asset_id for hit in selected], injected_ids=[hit.asset_id for hit in selected],
                injected_context=[text], outcome=outcome, trace_id=trace_id, turn_id=turn_id,
                query=query, tokens=tokens, error_code=error_code,
            ))
            connection.commit()
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.rollback()
            return AssetRetrievalResult(outcome="degraded", error_code="asset_event_write_failed",
                                        digest="sha256:" + hashlib.sha256(b"").hexdigest())
        finally:
            if connection is not None:
                connection.close()
    return AssetRetrievalResult(tuple(selected), text, tokens, outcome, error_code, digest, retrieval_id)
