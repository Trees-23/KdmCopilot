"""Rule-first intent routing for the memory scope.

The router deliberately stays deterministic and local.  It does not call an
LLM and it never treats a request to write or forget memory as permission to
mutate the memory index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

MEMORY_INTENTS: Final[tuple[str, ...]] = (
    "task",
    "history_query",
    "memory_write",
    "forget",
    "skill_discovery",
    "trace_query",
    "review",
)


@dataclass(frozen=True, slots=True)
class IntentDecision:
    """The normalized intent and the safe scope requested by a turn."""

    intent: str
    query: str
    scopes: tuple[str, ...]
    explicit: bool
    failure_mode: str

    @property
    def is_read_only(self) -> bool:
        return self.intent not in {"memory_write", "forget"}


class IntentRouter:
    """Classify user text using stable, reviewable rules."""

    _RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
        (
            "forget",
            ("forget", "delete from memory", "remove from memory", "tombstone", "忘记", "遗忘", "删除记忆"),
            ("memory", "wiki", "case", "trace"),
        ),
        (
            "memory_write",
            ("remember", "save to memory", "write to memory", "记住", "保存到记忆", "写入记忆"),
            ("memory", "wiki", "case"),
        ),
        (
            "trace_query",
            ("trace", "traces", "run history", "运行轨迹", "轨迹", "审计记录"),
            ("trace",),
        ),
        (
            "history_query",
            ("history", "previous conversation", "what did we discuss", "历史", "之前聊过", "上次"),
            ("memory", "history", "wiki", "case", "trace"),
        ),
        (
            "skill_discovery",
            ("skill", "skills", "how can you", "能力", "技能", "有没有工具"),
            ("skill",),
        ),
        (
            "review",
            ("review", "evaluate", "regression", "复盘", "评审", "评估", "回归"),
            ("case", "trace", "skill"),
        ),
    )

    def classify(self, text: str | None) -> IntentDecision:
        query = str(text or "").strip()
        folded = query.casefold()
        for intent, markers, scopes in self._RULES:
            if any(self._matches(folded, marker.casefold()) for marker in markers):
                return IntentDecision(
                    intent=intent,
                    query=query,
                    scopes=scopes,
                    explicit=True,
                    failure_mode="visible_error" if intent in {"history_query", "trace_query", "review"} else "no_injection",
                )
        return IntentDecision(
            intent="task",
            query=query,
            scopes=("memory", "wiki", "case", "skill", "trace"),
            explicit=False,
            failure_mode="session_only",
        )

    @staticmethod
    def _matches(text: str, marker: str) -> bool:
        if any("\u4e00" <= char <= "\u9fff" for char in marker):
            return marker in text
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(marker)}(?![a-z0-9_])", text))

    route = classify


def route_intent(text: str | None) -> IntentDecision:
    """Convenience wrapper for callers that do not need a router instance."""

    return IntentRouter().classify(text)


__all__ = ["IntentDecision", "IntentRouter", "MEMORY_INTENTS", "route_intent"]
