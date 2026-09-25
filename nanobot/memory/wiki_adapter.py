"""Adapters and capability discovery for external Wiki implementations.

The Wiki remains the Markdown/SQLite fact source owned by its provider.  This
module only defines the small protocol that memory synchronization needs and
normalizes entry-point or MCP capability metadata.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

from nanobot.agent.tools.context import ToolContext


class WikiAdapterError(RuntimeError):
    """Base error for an unavailable or incompatible Wiki adapter."""


class WikiUnavailableError(WikiAdapterError):
    """Raised when the configured Wiki provider is not available."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class WikiCapability:
    """One externally provided operation and its stable schema identity."""

    operation: str
    source: str
    name: str
    schema_digest: str
    version: str | None = None


@dataclass(frozen=True, slots=True)
class WikiPage:
    """A provider-owned page revision returned by an adapter."""

    page_id: str
    title: str
    content: str
    revision_id: str
    content_hash: str
    page_type: str = "fact"
    summary: str = ""
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    task_signature: Mapping[str, Any] = field(default_factory=dict)
    case_data: Mapping[str, Any] = field(default_factory=dict)
    intent: str = ""
    source_path: str = ""
    updated_at: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "WikiPage":
        """Normalize a Wiki/MCP page object without accepting missing identity."""

        page_id = str(payload.get("page_id") or payload.get("id") or "").strip()
        title = str(payload.get("title") or "").strip()
        content = str(payload.get("content") or "")
        computed_hash = _digest(content)
        supplied_hash = str(payload.get("content_hash") or computed_hash).strip()
        if supplied_hash != computed_hash:
            raise WikiAdapterError("Wiki page content_hash does not match content")
        content_hash = computed_hash
        revision_id = str(payload.get("revision_id") or payload.get("revision") or "").strip()
        if not revision_id:
            # Some MCP Wiki versions expose updated_at but not a separate revision ID.
            # Derive a stable adapter identity without pretending it is a new source of truth.
            revision_id = _digest(
                {"page_id": page_id, "content_hash": content_hash, "updated_at": payload.get("updated_at")}
            )
        if not page_id or not title:
            raise WikiAdapterError("Wiki page requires page_id and title")
        tags = tuple(str(value) for value in payload.get("tags", ()) or ())
        aliases = tuple(str(value) for value in payload.get("aliases", ()) or ())
        task_signature = payload.get("task_signature") or {}
        if not isinstance(task_signature, Mapping):
            raise WikiAdapterError("Wiki page task_signature must be an object")
        case_data = payload.get("case") or payload.get("case_data") or {}
        if not isinstance(case_data, Mapping):
            raise WikiAdapterError("Wiki page case_data must be an object")
        return cls(
            page_id=page_id,
            title=title,
            content=content,
            revision_id=revision_id,
            content_hash=content_hash,
            page_type=str(payload.get("page_type") or payload.get("type") or "fact"),
            summary=str(payload.get("summary") or ""),
            tags=tags,
            aliases=aliases,
            task_signature=dict(task_signature),
            case_data=dict(case_data),
            intent=str(payload.get("intent") or ""),
            source_path=str(payload.get("source_path") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )

    @classmethod
    def from_provider(cls, value: Any) -> "WikiPage":
        """Normalize a provider dataclass or mapping without importing its types."""

        if isinstance(value, Mapping):
            return cls.from_payload(value)
        content = str(getattr(value, "content", ""))
        updated_at = str(getattr(value, "updated_at", ""))
        content_hash = _digest(content)
        page_id = str(getattr(value, "id", "") or getattr(value, "page_id", ""))
        return cls.from_payload(
            {
                "page_id": page_id,
                "title": getattr(value, "title", ""),
                "content": content,
                "revision_id": getattr(value, "revision_id", "")
                or _digest({"page_id": page_id, "content_hash": content_hash, "updated_at": updated_at}),
                "content_hash": content_hash,
                "page_type": getattr(value, "page_type", "fact"),
                "tags": getattr(value, "tags", ()),
                "aliases": getattr(value, "aliases", ()),
                "updated_at": updated_at,
            }
        )


@dataclass(frozen=True, slots=True)
class WikiRelation:
    from_page_id: str
    to_page_id: str
    relation_type: str = "related"
    revision_id: str | None = None


class WikiAdapter(Protocol):
    """Minimum async protocol implemented by an entry-point or MCP provider."""

    def capabilities(self) -> tuple[WikiCapability, ...]: ...

    async def search(self, query: str, *, limit: int = 5) -> list[WikiPage]: ...

    async def read(self, selector: str) -> WikiPage | None: ...

    async def upsert(self, page: Mapping[str, Any]) -> WikiPage: ...

    async def link(self, relation: WikiRelation) -> WikiRelation: ...

    async def unlink(self, relation: WikiRelation) -> int: ...

    async def forget(self, selector: str, *, archive: bool = True) -> WikiPage: ...


def _operation_for_name(name: str) -> str | None:
    normalized = name.rsplit(".", 1)[-1].lower()
    aliases = {
        "wiki_search": "search",
        "wiki_read": "read",
        "wiki_upsert": "upsert",
        "wiki_link": "link",
        "wiki_unlink": "unlink",
        "wiki_forget": "forget",
        "knowledge_search": "search",
        "knowledge_read": "read",
        "knowledge_upsert": "upsert",
        "knowledge_link": "link",
        "knowledge_unlink": "unlink",
        "knowledge_forget": "forget",
    }
    return aliases.get(normalized)


def discover_entry_point_capabilities(
    points: Iterable[EntryPoint] | None = None,
) -> tuple[WikiCapability, ...]:
    """Discover Wiki tools without importing or mutating the provider."""

    selected = points if points is not None else entry_points(group="nanobot.tools")
    capabilities: list[WikiCapability] = []
    for point in selected:
        operation = _operation_for_name(point.name)
        if operation is None:
            continue
        schema = getattr(point, "schema", None) or getattr(point, "parameters", None)
        schema_digest = _digest(schema if schema is not None else {"entry_point": point.value})
        version: str | None = None
        try:
            if point.dist is not None:
                version = point.dist.version
        except Exception:
            version = None
        capabilities.append(
            WikiCapability(
                operation=operation,
                source="entrypoint",
                name=point.name,
                schema_digest=schema_digest,
                version=version,
            )
        )
    return tuple(sorted(capabilities, key=lambda item: (item.operation, item.name)))


def discover_mcp_capabilities(tools: Iterable[Any]) -> tuple[WikiCapability, ...]:
    """Normalize MCP ``tools/list`` data into the same capability model."""

    capabilities: list[WikiCapability] = []
    for tool in tools:
        if isinstance(tool, Mapping):
            name = str(tool.get("name") or "")
            schema = tool.get("inputSchema") or tool.get("input_schema") or tool.get("schema") or {}
        else:
            name = str(getattr(tool, "name", ""))
            schema = (
                getattr(tool, "inputSchema", None)
                or getattr(tool, "input_schema", None)
                or getattr(tool, "schema", None)
                or {}
            )
        operation = _operation_for_name(name)
        if operation is None:
            continue
        capabilities.append(
            WikiCapability(
                operation=operation,
                source="mcp",
                name=name,
                schema_digest=_digest(schema),
            )
        )
    return tuple(sorted(capabilities, key=lambda item: (item.operation, item.name)))


def capability_digest(capabilities: Iterable[WikiCapability]) -> str:
    """Return a stable digest for capability/version changes."""

    return _digest(
        [
            {
                "operation": item.operation,
                "source": item.source,
                "name": item.name,
                "schema_digest": item.schema_digest,
                "version": item.version,
            }
            for item in capabilities
        ]
    )


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class EntryPointWikiAdapter:
    """Call Wiki ``nanobot.tools`` entry points through the normal Tool API."""

    def __init__(self, workspace: str | Path, points: Iterable[EntryPoint] | None = None):
        self.workspace = str(Path(workspace).resolve())
        self._points = tuple(points) if points is not None else tuple(entry_points(group="nanobot.tools"))
        self._tools: dict[str, Any] | None = None

    def capabilities(self) -> tuple[WikiCapability, ...]:
        return discover_entry_point_capabilities(self._points)

    def _load_tools(self) -> dict[str, Any]:
        if self._tools is not None:
            return self._tools
        loaded: dict[str, Any] = {}
        context = ToolContext(config=None, workspace=self.workspace)
        for point in self._points:
            if _operation_for_name(point.name) is None:
                continue
            try:
                tool_type = point.load()
                tool = tool_type.create(context) if hasattr(tool_type, "create") else tool_type()
                loaded[point.name] = tool
            except Exception as exc:
                raise WikiUnavailableError(f"failed to load Wiki entry point {point.name}") from exc
        self._tools = loaded
        return loaded

    def _provider_store(self) -> Any | None:
        for tool in self._load_tools().values():
            store = getattr(tool, "store", None)
            if store is not None:
                return store
        return None

    async def _call(self, operation: str, **kwargs: Any) -> Any:
        tools = self._load_tools()
        tool = next(
            (value for name, value in tools.items() if _operation_for_name(name) == operation), None
        )
        if tool is None:
            raise WikiUnavailableError(f"Wiki entry-point operation is unavailable: {operation}")
        return await _maybe_await(tool.execute(**kwargs))

    async def search(self, query: str, *, limit: int = 5) -> list[WikiPage]:
        store = self._provider_store()
        if store is not None:
            return [WikiPage.from_provider(item.page) for item in store.search(query, limit=limit)]
        payload = await self._call("search", query=query, limit=limit)
        return [WikiPage.from_provider(item) for item in payload]

    async def read(self, selector: str) -> WikiPage | None:
        store = self._provider_store()
        if store is not None:
            page = store.get_page(selector)
            return WikiPage.from_provider(page) if page else None
        payload = await self._call("read", selector=selector)
        return WikiPage.from_provider(payload) if payload else None

    async def upsert(self, page: Mapping[str, Any]) -> WikiPage:
        store = self._provider_store()
        if store is not None:
            result = store.upsert_page(**dict(page))
            return WikiPage.from_provider(result)
        payload = await self._call("upsert", **dict(page))
        return WikiPage.from_provider(payload)

    async def link(self, relation: WikiRelation) -> WikiRelation:
        store = self._provider_store()
        if store is not None:
            store.link_pages(relation.from_page_id, relation.to_page_id, relation.relation_type)
            return relation
        payload = await self._call(
            "link",
            from_selector=relation.from_page_id,
            to_selector=relation.to_page_id,
            relation=relation.relation_type,
        )
        return WikiRelation(
            from_page_id=str(payload.get("from_page_id") or relation.from_page_id),
            to_page_id=str(payload.get("to_page_id") or relation.to_page_id),
            relation_type=str(payload.get("relation") or relation.relation_type),
            revision_id=payload.get("revision_id"),
        )

    async def unlink(self, relation: WikiRelation) -> int:
        store = self._provider_store()
        if store is not None:
            _from, _to, removed = store.unlink_pages(
                relation.from_page_id, relation.to_page_id, relation.relation_type
            )
            return int(removed)
        payload = await self._call(
            "unlink",
            from_selector=relation.from_page_id,
            to_selector=relation.to_page_id,
            relation=relation.relation_type,
        )
        return int(payload.get("removed", 0)) if isinstance(payload, Mapping) else 1

    async def forget(self, selector: str, *, archive: bool = True) -> WikiPage:
        store = self._provider_store()
        if store is not None:
            return WikiPage.from_provider(store.forget_page(selector, archive=archive))
        payload = await self._call("forget", selector=selector, archive=archive)
        return WikiPage.from_provider(payload)


class McpWikiAdapter:
    """Small MCP adapter around an existing async ``call_tool`` function."""

    def __init__(
        self,
        call_tool: Callable[[str, dict[str, Any]], Awaitable[Any]],
        tools: Iterable[Any],
    ):
        self._call_tool = call_tool
        self._tools = tuple(tools)

    def capabilities(self) -> tuple[WikiCapability, ...]:
        return discover_mcp_capabilities(self._tools)

    def _name(self, operation: str) -> str:
        for capability in self.capabilities():
            if capability.operation == operation:
                return capability.name
        raise WikiUnavailableError(f"MCP Wiki operation is unavailable: {operation}")

    async def _call(self, operation: str, **arguments: Any) -> Any:
        return await self._call_tool(self._name(operation), arguments)

    @staticmethod
    def _page_payload(payload: Any) -> Mapping[str, Any] | None:
        if payload is None:
            return None
        if isinstance(payload, Mapping) and isinstance(payload.get("page"), Mapping):
            return payload["page"]
        return payload if isinstance(payload, Mapping) else None

    async def search(self, query: str, *, limit: int = 5) -> list[WikiPage]:
        payload = await self._call("search", query=query, limit=limit)
        rows = payload.get("results", []) if isinstance(payload, Mapping) else payload
        return [WikiPage.from_payload(item) for item in rows or []]

    async def read(self, selector: str) -> WikiPage | None:
        return (
            WikiPage.from_payload(page)
            if (page := self._page_payload(await self._call("read", selector=selector)))
            else None
        )

    async def upsert(self, page: Mapping[str, Any]) -> WikiPage:
        result = self._page_payload(await self._call("upsert", **dict(page)))
        if result is None:
            raise WikiAdapterError("MCP Wiki upsert returned no page")
        return WikiPage.from_payload(result)

    async def link(self, relation: WikiRelation) -> WikiRelation:
        payload = await self._call(
            "link",
            from_selector=relation.from_page_id,
            to_selector=relation.to_page_id,
            relation=relation.relation_type,
        )
        return WikiRelation(
            relation.from_page_id,
            relation.to_page_id,
            str(payload.get("relation") or relation.relation_type),
        )

    async def unlink(self, relation: WikiRelation) -> int:
        payload = await self._call(
            "unlink",
            from_selector=relation.from_page_id,
            to_selector=relation.to_page_id,
            relation=relation.relation_type,
        )
        return int(payload.get("removed", 0)) if isinstance(payload, Mapping) else 1

    async def forget(self, selector: str, *, archive: bool = True) -> WikiPage:
        result = self._page_payload(await self._call("forget", selector=selector, archive=archive))
        if result is None:
            raise WikiAdapterError("MCP Wiki forget returned no page")
        return WikiPage.from_payload(result)


__all__ = [
    "EntryPointWikiAdapter",
    "McpWikiAdapter",
    "WikiAdapter",
    "WikiAdapterError",
    "WikiCapability",
    "WikiPage",
    "WikiRelation",
    "WikiUnavailableError",
    "capability_digest",
    "discover_entry_point_capabilities",
    "discover_mcp_capabilities",
]
