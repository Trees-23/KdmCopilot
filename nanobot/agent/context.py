"""Context builder for assembling agent prompts."""

import base64
import hashlib
import json
import mimetypes
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools import mcp as mcp_tools
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.apps.cli import utils as cli_app_utils
from nanobot.bus.events import InboundMessage
from nanobot.memory.retriever import MemoryRetriever, RetrievalResult
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_END,
    RUNTIME_CONTEXT_MESSAGE_META,
    RUNTIME_CONTEXT_TAG,
    RuntimeContextBlock,
    append_runtime_context,
)
from nanobot.utils.helpers import (
    detect_image_mime,
    estimate_message_tokens,
    load_bundled_template,
    truncate_text_to_tokens,
)
from nanobot.utils.prompt_templates import render_template

SYSTEM_CONTEXT_SECTIONS_META = "system_context_sections"
SYSTEM_CONTEXT_SECTIONS_VERSION = 1


@dataclass(frozen=True, slots=True)
class SystemPromptSections:
    """Stable instruction prefix and dynamic facts for one system prompt."""

    stable: str
    dynamic: str
    retrieval: RetrievalResult | None = None

    @property
    def content(self) -> str:
        return _join_prompt_sections(self.stable, self.dynamic)

    def metadata(self) -> dict[str, int | str]:
        metadata: dict[str, int | str] = {
            "schema_version": SYSTEM_CONTEXT_SECTIONS_VERSION,
            "stable_chars": len(self.stable),
            "stable_system_digest": _digest_text(self.stable),
            "dynamic_fact_digest": _digest_text(self.dynamic),
            "stable_tokens": estimate_message_tokens({"role": "system", "content": self.stable}),
            "dynamic_tokens": estimate_message_tokens({"role": "system", "content": self.dynamic}),
        }
        if self.retrieval is not None:
            metadata.update(
                {
                    "retrieval_digest": self.retrieval.digest,
                    "retrieval_intent": self.retrieval.intent,
                    "retrieval_outcome": self.retrieval.outcome,
                    "retrieval_tokens": self.retrieval.tokens,
                }
            )
        return metadata


def model_request_context_cache_metadata(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    overlay_tokens: int = 0,
) -> dict[str, int | str]:
    """Return bounded cache diagnostics without retaining any prompt body."""
    sections = _system_sections_metadata_from_messages(messages)
    if sections is None:
        system_text = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system"
        )
        sections = {
            "schema_version": 0,
            "stable_system_digest": _digest_text(system_text),
            "dynamic_fact_digest": _digest_text(""),
            "stable_tokens": estimate_message_tokens({"role": "system", "content": system_text}),
            "dynamic_tokens": 0,
        }
    return {
        **sections,
        "tool_schema_digest": tool_schema_digest(tools),
        "overlay_tokens": max(0, int(overlay_tokens)),
        "cached_tokens": "unknown",
    }


def tool_schema_digest(tools: list[dict[str, Any]] | None) -> str:
    """Hash complete tool schemas; stable ordering alone is not a schema version."""
    raw = json.dumps(tools or [], ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _system_sections_metadata_from_messages(
    messages: list[dict[str, Any]],
) -> dict[str, int | str] | None:
    for message in messages:
        if message.get("role") != "system":
            continue
        meta = message.get("_meta")
        value = meta.get(SYSTEM_CONTEXT_SECTIONS_META) if isinstance(meta, Mapping) else None
        if not isinstance(value, Mapping):
            continue
        if value.get("schema_version") != SYSTEM_CONTEXT_SECTIONS_VERSION:
            continue
        stable_digest = value.get("stable_system_digest")
        dynamic_digest = value.get("dynamic_fact_digest")
        if not isinstance(stable_digest, str) or not isinstance(dynamic_digest, str):
            continue
        result: dict[str, int | str] = {
            "schema_version": SYSTEM_CONTEXT_SECTIONS_VERSION,
            "stable_system_digest": stable_digest,
            "dynamic_fact_digest": dynamic_digest,
            "stable_tokens": _non_negative_int(value.get("stable_tokens")),
            "dynamic_tokens": _non_negative_int(value.get("dynamic_tokens")),
        }
        for key in ("retrieval_digest", "retrieval_intent", "retrieval_outcome"):
            item = value.get(key)
            if isinstance(item, str):
                result[key] = item
        if "retrieval_tokens" in value:
            result["retrieval_tokens"] = _non_negative_int(value.get("retrieval_tokens"))
        return result
    return None


def _join_prompt_sections(stable: str, dynamic: str) -> str:
    return "\n\n---\n\n".join(part for part in (stable, dynamic) if part)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return persisted kwargs for turn-attached capabilities."""
    return cli_app_utils.session_extra(metadata) | mcp_tools.session_extra(metadata)


async def connect_mcp(state: Any, tools: ToolRegistry) -> None:
    await mcp_tools.connect_missing_servers(state, tools)


async def close_mcp(state: Any) -> None:
    await mcp_tools.close_mcp_servers(state)


async def handle_runtime_control(state: Any, msg: InboundMessage, tools: ToolRegistry) -> bool:
    return await mcp_tools.handle_runtime_control(state, msg, tools)


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]
    _RUNTIME_CONTEXT_TAG = RUNTIME_CONTEXT_TAG
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_TOKENS = 8_000  # hard cap on recent history section size (tokens)
    _RUNTIME_CONTEXT_END = RUNTIME_CONTEXT_END

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)
        self.memory_retriever = MemoryRetriever(workspace)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
        memory_query: str | None = None,
        trace_id: str | None = None,
        turn_id: str | None = None,
        context_window_tokens: int | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        return self.build_system_sections(
            skill_names,
            channel=channel,
            session_summary=session_summary,
            workspace=workspace,
            include_memory_recent_history=include_memory_recent_history,
            session_key=session_key,
            unified_session=unified_session,
            memory_query=memory_query,
            trace_id=trace_id,
            turn_id=turn_id,
            context_window_tokens=context_window_tokens,
        ).content

    def build_system_sections(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
        memory_query: str | None = None,
        trace_id: str | None = None,
        turn_id: str | None = None,
        context_window_tokens: int | None = None,
    ) -> SystemPromptSections:
        """Build stable instructions separately from dynamic memory facts."""
        root = workspace or self.workspace
        stable_parts = [self._get_identity(channel=channel, workspace=root)]

        bootstrap = self._load_bootstrap_files(root)
        if bootstrap:
            stable_parts.append(bootstrap)

        stable_parts.append(render_template("agent/tool_contract.md"))

        dynamic_parts: list[str] = []
        retrieval: RetrievalResult | None = None

        if memory_query and session_key:
            retrieval = self.memory_retriever.retrieve(
                memory_query,
                session_key=session_key,
                workspace=root,
                trace_id=trace_id,
                turn_id=turn_id,
                context_window_tokens=context_window_tokens,
            )
            rendered_retrieval = retrieval.render()
            if rendered_retrieval:
                dynamic_parts.append(rendered_retrieval)

        memory = self.memory.get_memory_context()
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            dynamic_parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                stable_parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            stable_parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        if include_memory_recent_history:
            entries = self.memory.read_recent_history_for_prompt(
                since_cursor=self.memory.get_last_dream_cursor(),
                session_key=session_key,
                unified_session=unified_session,
            )
            if entries:
                capped = entries[-self._MAX_RECENT_HISTORY:]
                history_text = "\n".join(
                    f"- [{e['timestamp']}] {e['content']}" for e in capped
                )
                history_text = truncate_text_to_tokens(history_text, self._MAX_HISTORY_TOKENS)
                dynamic_parts.append("# Recent History\n\n" + history_text)

        if session_summary:
            dynamic_parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return SystemPromptSections(
            stable="\n\n---\n\n".join(stable_parts),
            dynamic="\n\n---\n\n".join(dynamic_parts),
            retrieval=retrieval,
        )

    def _get_identity(self, channel: str | None = None, workspace: Path | None = None) -> str:
        """Get the core identity section."""
        root = workspace or self.workspace
        workspace_path = str(root.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            if not left:
                return right
            if not right:
                return left
            return f"{left}\n\n{right}"

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self, workspace: Path | None = None) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        root = workspace or self.workspace

        for filename in self.BOOTSTRAP_FILES:
            file_path = root / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        tpl = load_bundled_template(template_path)
        if tpl is not None:
            return content.strip() == tpl.strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        runtime_context_blocks: Sequence[RuntimeContextBlock] | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
        trace_id: str | None = None,
        turn_id: str | None = None,
        context_window_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        root = workspace or self.workspace
        user_content = self._build_user_content(current_message, media)
        blocks = list(runtime_context_blocks or ()) if current_role == "user" else []
        merged, runtime_context_meta = append_runtime_context(user_content, blocks)
        sections = self.build_system_sections(
            skill_names,
            channel=channel,
            session_summary=session_summary,
            workspace=root,
            include_memory_recent_history=include_memory_recent_history,
            session_key=session_key,
            unified_session=unified_session,
            memory_query=current_message,
            trace_id=trace_id,
            turn_id=turn_id,
            context_window_tokens=context_window_tokens,
        )
        messages = [
            {
                "role": "system",
                "content": sections.content,
                "_meta": {SYSTEM_CONTEXT_SECTIONS_META: sections.metadata()},
            },
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            if current_role == "user" and runtime_context_meta is not None:
                internal_meta = dict(last.get("_meta") or {})
                internal_meta[RUNTIME_CONTEXT_MESSAGE_META] = runtime_context_meta
                last["_meta"] = internal_meta
            messages[-1] = last
            return messages
        current = {"role": current_role, "content": merged}
        if current_role == "user" and runtime_context_meta is not None:
            current["_meta"] = {RUNTIME_CONTEXT_MESSAGE_META: runtime_context_meta}
        messages.append(current)
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
