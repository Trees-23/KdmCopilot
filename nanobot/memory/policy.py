"""Read-only memory scope policy used by ContextBuilder retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from nanobot.memory.intent import IntentDecision


class ToolPolicyError(PermissionError):
    """Raised when a tool is outside the active agent role's allowlist."""


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """A bounded retrieval policy for one intent."""

    intent: str
    scopes: tuple[str, ...]
    allow_full_content: bool = False
    explicit: bool = False
    failure_mode: str = "session_only"

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


class MemoryScopePolicy:
    """Convert an intent decision into an immutable, read-only scope."""

    _ALIASES = {"skills": "skill", "cases": "case", "traces": "trace", "history": "memory"}

    def for_intent(self, decision: IntentDecision) -> MemoryScope:
        scopes = tuple(dict.fromkeys(self._ALIASES.get(scope, scope) for scope in decision.scopes))
        return MemoryScope(
            intent=decision.intent,
            scopes=scopes,
            allow_full_content=False,
            explicit=decision.explicit,
            failure_mode=decision.failure_mode,
        )

    scope_for = for_intent

    @staticmethod
    def normalize(scopes: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(MemoryScopePolicy._ALIASES.get(str(scope), str(scope)) for scope in scopes))


MemoryPolicy = MemoryScopePolicy


class ToolPolicy:
    """Fail-closed role matrix evaluated before tool parameter execution."""

    _DENIED_BY_ROLE = {
        "maintenance": {
            "write_file", "edit_file", "apply_patch", "exec", "write_stdin", "git",
            "message", "cron", "create_goal", "update_goal", "skill_propose",
        },
        "evaluation": {
            "write_file", "edit_file", "apply_patch", "exec", "write_stdin", "git",
            "message", "cron", "create_goal", "update_goal", "skill_propose",
        },
    }
    _HIGH_RISK_PREFIXES = ("mcp_",)

    def __init__(self, *, role: str = "agent", configured: bool = True) -> None:
        self.role = role
        self.configured = configured

    def check(self, tool_name: str, *, role: str | None = None) -> None:
        active_role = role or self.role
        if not self.configured:
            raise ToolPolicyError("ToolPolicy is not configured; refusing tool call")
        denied = self._DENIED_BY_ROLE.get(active_role, set())
        if tool_name in denied or (active_role in {"maintenance", "evaluation"} and tool_name.startswith(self._HIGH_RISK_PREFIXES)):
            raise ToolPolicyError(f"tool '{tool_name}' is denied for role '{active_role}'")

    def allows(self, tool_name: str, *, role: str | None = None) -> bool:
        try:
            self.check(tool_name, role=role)
        except ToolPolicyError:
            return False
        return True


__all__ = ["MemoryPolicy", "MemoryScope", "MemoryScopePolicy", "ToolPolicy", "ToolPolicyError"]
