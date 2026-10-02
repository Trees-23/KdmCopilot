"""Bounded, read-only retrieval from the derived memory SQLite index."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from nanobot.memory.db import connect_memory_db
from nanobot.memory.intent import IntentDecision, IntentRouter
from nanobot.memory.policy import MemoryScope, MemoryScopePolicy
from nanobot.memory.retrieval_events import RetrievalEvent, record_retrieval
from nanobot.utils.helpers import estimate_message_tokens, truncate_text_to_tokens

SOFT_TOKEN_BUDGET = 2_000
HARD_TOKEN_BUDGET = 3_000
ADAPTIVE_BUDGET_RATIO = 0.06


@dataclass(frozen=True, slots=True)
class MemoryHit:
    object_id: str
    object_type: str
    title: str
    summary: str
    revision_id: str | None
    source_ref: str | None
    score: float
    freshness: float
    authority: float

    @property
    def identifier(self) -> str:
        return f"{self.object_type}:{self.object_id}"


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    intent: str
    scopes: tuple[str, ...]
    hits: tuple[MemoryHit, ...]
    outcome: str
    error_code: str | None
    digest: str
    tokens: int
    soft_budget: int
    hard_budget: int

    @property
    def injected_ids(self) -> tuple[str, ...]:
        return tuple(hit.identifier for hit in self.hits)

    def render(self) -> str:
        if not self.hits:
            if self.error_code and self.intent in {"history_query", "trace_query", "review"}:
                return f"[Memory retrieval unavailable: {self.error_code}]"
            return ""
        lines = ["# Retrieved Memory (read-only)"]
        for hit in self.hits:
            revision = f" revision={hit.revision_id}" if hit.revision_id else ""
            source = f" source={hit.source_ref}" if hit.source_ref else ""
            lines.append(f"- [{hit.object_type}] {hit.title}{revision}{source}: {hit.summary}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    query: str
    session_key: str
    workspace: Path
    trace_id: str | None = None
    turn_id: str | None = None
    context_window_tokens: int | None = None
    decision: IntentDecision | None = None


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json_value(value: Any, default: Any = None) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _match_score(query: str, title: str, summary: str, base: float = 0.0) -> float:
    terms = [term.casefold() for term in query.split() if term.strip()]
    haystack = f"{title} {summary}".casefold()
    matched = sum(1 for term in terms if term in haystack)
    return base + (matched / max(1, len(terms)))


def _matches_query(query: str, title: str, summary: str) -> bool:
    terms = [term.casefold() for term in query.split() if term.strip()]
    if not terms:
        return True
    haystack = f"{title} {summary}".casefold()
    return any(term in haystack for term in terms)


class MemoryRetriever:
    """Search only derived rows and return bounded summaries with references."""

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        router: IntentRouter | None = None,
        policy: MemoryScopePolicy | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser() if workspace is not None else None
        self.router = router or IntentRouter()
        self.policy = policy or MemoryScopePolicy()

    def retrieve(
        self,
        query: str,
        *,
        session_key: str = "",
        workspace: str | Path | None = None,
        trace_id: str | None = None,
        turn_id: str | None = None,
        context_window_tokens: int | None = None,
        decision: IntentDecision | None = None,
        limit: int = 32,
    ) -> RetrievalResult:
        root = Path(workspace or self.workspace or "").expanduser()
        decision = decision or self.router.classify(query)
        scope = self.policy.for_intent(decision)
        soft, hard = self._budgets(context_window_tokens)
        if decision.intent in {"memory_write", "forget"}:
            result = self._result(decision, scope, (), "skipped", None, soft, hard)
            self._record(result, session_key, trace_id, turn_id, query, root)
            return result
        db_path = root / ".nanobot" / "memory.sqlite3"
        if not root.is_dir() or not db_path.is_file():
            result = self._result(decision, scope, (), "degraded", "memory_db_unavailable", soft, hard)
            self._record(result, session_key, trace_id, turn_id, query, root)
            return result
        try:
            connection = connect_memory_db(root)
            hits = self._search(connection, root, query, scope, limit, session_key)
            connection.close()
            selected = self._fit_budget(hits, hard)
            outcome = "indexed" if selected else "empty"
            result = self._result(decision, scope, selected, outcome, None, soft, hard)
            self._record(result, session_key, trace_id, turn_id, query, root)
            return result
        except (OSError, sqlite3.Error, ValueError):
            result = self._result(decision, scope, (), "degraded", "memory_query_failed", soft, hard)
            self._record(result, session_key, trace_id, turn_id, query, root)
            return result

    def _search(
        self,
        connection: sqlite3.Connection,
        workspace: Path,
        query: str,
        scope: MemoryScope,
        limit: int,
        session_key: str = "",
    ) -> list[MemoryHit]:
        now = datetime.now(UTC)
        hits: list[MemoryHit] = []
        # SQLite filtering is intentionally broad; token matching below handles
        # multi-word requests without requiring the complete sentence in a row.
        needle = "%"
        if scope.allows("memory"):
            rows = connection.execute(
                """SELECT memory_id,memory_type,title,summary,current_revision_id,source_refs_json,
                          effective_score,authority,updated_at
                   FROM memory_records
                   WHERE status NOT IN ('archived','tombstone') AND deletion_state='none'
                     AND (title LIKE ? OR summary LIKE ? OR tags_json LIKE ?)
                   ORDER BY effective_score DESC,updated_at DESC LIMIT ?""",
                (needle, needle, needle, limit),
            )
            hits.extend(
                self._memory_hits(
                    (row for row in rows if _matches_query(query, str(row[2]), str(row[3]))), now
                )
            )
        if scope.allows("wiki") or scope.allows("case"):
            rows = connection.execute(
                """SELECT p.page_id,p.page_type,p.title,p.summary,p.current_revision_id,p.source_path,
                          p.updated_at
                   FROM wiki_pages p
                   WHERE p.status NOT IN ('archived','tombstone')
                     AND (? IN ('case','all') OR p.page_type != 'case')
                     AND (p.title LIKE ? OR p.summary LIKE ? OR p.tags_json LIKE ?)
                     AND NOT EXISTS (SELECT 1 FROM tombstones t
                                     WHERE t.object_type='wiki_page' AND t.object_id=p.page_id)
                   ORDER BY p.updated_at DESC LIMIT ?""",
                ("case" if scope.allows("case") and not scope.allows("wiki") else "all", needle, needle, needle, limit),
            )
            for row in rows:
                if row[1] == "case" and not scope.allows("case"):
                    continue
                if row[1] != "case" and not scope.allows("wiki"):
                    continue
                if _matches_query(query, str(row[2]), str(row[3])):
                    hits.append(self._hit_from_row("case" if row[1] == "case" else "wiki", row, now, base=0.2, query=query))
        if scope.allows("skill"):
            rows = connection.execute(
                """SELECT skill_id,name,description,current_revision_id,source_path,updated_at
                   FROM skills WHERE status='active' AND (name LIKE ? OR description LIKE ?)
                   ORDER BY updated_at DESC LIMIT ?""",
                (needle, needle, limit),
            )
            for row in rows:
                if _matches_query(query, str(row[1]), str(row[2])):
                    hits.append(
                        self._hit_from_row(
                            "skill", row, now, base=0.15, query=query,
                            title_index=1, summary_index=2, revision_index=3,
                            source_index=4, freshness_index=5,
                        )
                    )
        if scope.allows("trace"):
            rows = connection.execute(
                """SELECT trace_id,session_key,summary,summary,NULL,source_path,
                          started_at,tool_count,event_count
                   FROM trace_index
                   WHERE workspace=? AND index_status='indexed'
                     AND (trace_expire_at IS NULL OR trace_expire_at > ?)
                     AND (?='' OR session_key=?)
                     AND (summary LIKE ? OR outcome LIKE ?)
                   ORDER BY started_at DESC LIMIT ?""",
                (str(workspace.resolve()), now.isoformat(), session_key, session_key, needle, needle, limit),
            )
            for row in rows:
                if _matches_query(query, str(row[2]), str(row[3])):
                    hits.append(self._hit_from_row("trace", row, now, base=0.1, query=query))
        deduped = {hit.identifier: hit for hit in hits}
        return sorted(deduped.values(), key=lambda hit: (-hit.score, -hit.freshness, hit.identifier))[:limit]

    @staticmethod
    def _memory_hits(rows: Iterable[sqlite3.Row], now: datetime) -> list[MemoryHit]:
        hits: list[MemoryHit] = []
        for row in rows:
            freshness = MemoryRetriever._freshness(row[8], now)
            authority = _safe_float(row[7])
            hits.append(
                MemoryHit(
                    object_id=str(row[0]), object_type=f"memory:{row[1]}", title=str(row[2]), summary=str(row[3]),
                    revision_id=row[4], source_ref=next(iter(_json_value(row[5], []) or []), None),
                    score=_safe_float(row[6]) + 0.15 * freshness + 0.1 * authority,
                    freshness=freshness, authority=authority,
                )
            )
        return hits

    @staticmethod
    def _hit_from_row(
        object_type: str,
        row: sqlite3.Row,
        now: datetime,
        *,
        base: float,
        query: str = "",
        title_index: int = 2,
        summary_index: int = 3,
        revision_index: int = 4,
        source_index: int = 5,
        freshness_index: int = 6,
    ) -> MemoryHit:
        title = str(row[title_index] if len(row) > title_index else row[1])
        summary = str(row[summary_index] if len(row) > summary_index else "")
        source = str(row[source_index]) if len(row) > source_index and row[source_index] else None
        freshness = MemoryRetriever._freshness(row[freshness_index] if len(row) > freshness_index else None, now)
        authority = 0.5
        return MemoryHit(
            object_id=str(row[0]), object_type=object_type, title=title, summary=summary[:2000],
            revision_id=str(row[revision_index]) if len(row) > revision_index and row[revision_index] else None,
            source_ref=source,
            score=_match_score(query, title, summary, base) + 0.15 * freshness + 0.1 * authority,
            freshness=freshness, authority=authority,
        )

    @staticmethod
    def _freshness(value: Any, now: datetime) -> float:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            age_days = max(0.0, (now - timestamp.astimezone(UTC)).total_seconds() / 86400)
            return 1.0 / (1.0 + age_days / 30.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _budgets(context_window_tokens: int | None) -> tuple[int, int]:
        adaptive = int(context_window_tokens * ADAPTIVE_BUDGET_RATIO) if context_window_tokens else HARD_TOKEN_BUDGET
        adaptive = max(128, adaptive)
        soft = min(SOFT_TOKEN_BUDGET, adaptive)
        hard = min(HARD_TOKEN_BUDGET, max(soft, adaptive))
        return soft, hard

    @staticmethod
    def _fit_budget(hits: Iterable[MemoryHit], hard_budget: int) -> tuple[MemoryHit, ...]:
        selected: list[MemoryHit] = []
        token_count = 0
        for hit in hits:
            summary = truncate_text_to_tokens(hit.summary, 180)
            candidate = replace(hit, summary=summary)
            estimate = estimate_message_tokens({"role": "system", "content": f"{candidate.title}: {candidate.summary}"})
            if token_count + estimate > hard_budget:
                break
            selected.append(candidate)
            token_count += estimate
        return tuple(selected)

    @staticmethod
    def _result(
        decision: IntentDecision,
        scope: MemoryScope,
        hits: tuple[MemoryHit, ...],
        outcome: str,
        error_code: str | None,
        soft: int,
        hard: int,
    ) -> RetrievalResult:
        digest = _digest([(hit.identifier, hit.revision_id, hit.summary) for hit in hits])
        tokens = estimate_message_tokens({"role": "system", "content": "\n".join(hit.summary for hit in hits)})
        return RetrievalResult(decision.intent, scope.scopes, hits, outcome, error_code, digest, tokens, soft, hard)

    @staticmethod
    def _record(
        result: RetrievalResult,
        session_key: str,
        trace_id: str | None,
        turn_id: str | None,
        query: str,
        workspace: Path,
    ) -> None:
        if not workspace or not workspace.is_dir():
            return
        db_path = workspace / ".nanobot" / "memory.sqlite3"
        if not db_path.is_file():
            return
        try:
            connection = connect_memory_db(workspace)
            record_retrieval(
                connection,
                RetrievalEvent(
                    session_key=session_key or "unknown",
                    intent=result.intent,
                    scopes=list(result.scopes),
                    result_ids=list(result.injected_ids),
                    injected_ids=list(result.injected_ids),
                    injected_context=[result.digest],
                    outcome=result.outcome,
                    trace_id=trace_id,
                    turn_id=turn_id,
                    query=query,
                    tokens=result.tokens,
                    error_code=result.error_code,
                ),
            )
            connection.commit()
            connection.close()
        except (OSError, sqlite3.Error):
            return


__all__ = [
    "ADAPTIVE_BUDGET_RATIO", "HARD_TOKEN_BUDGET", "MemoryHit", "MemoryRetriever",
    "RetrievalRequest", "RetrievalResult", "SOFT_TOKEN_BUDGET",
]
