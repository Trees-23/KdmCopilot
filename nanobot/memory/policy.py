"""Read-only memory scope policy used by ContextBuilder retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from nanobot.memory.intent import IntentDecision


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

__all__ = ["MemoryPolicy", "MemoryScope", "MemoryScopePolicy"]
