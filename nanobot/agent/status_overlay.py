"""Transient, request-scoped Agent Status facts.

The rendered status is deliberately never stored.  Only the small failure
ledger lives in session metadata so a continuation can retain the logical
user-request boundary after a process restart.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Mapping, MutableMapping

from nanobot.agent.hook import AgentHook, AgentHookContext, ToolAuditOutcome
from nanobot.audit.diagnostics import tool_operation_evidence
from nanobot.audit.ids import new_audit_id

if TYPE_CHECKING:
    from nanobot.session.manager import Session, SessionManager


AGENT_STATUS_FAILURE_LEDGER_KEY = "_agent_status_failure_ledger"
LOGICAL_USER_REQUEST_ID_META = "_logical_user_request_id"
STATUS_SCHEMA_VERSION = 1
_MAX_SOURCE_EVENT_IDS = 8
_MAX_RENDERED_FAILURES = 4

OverlayResult = Literal["applied", "omitted_unsupported", "omitted_invalid"]


@dataclass(frozen=True, slots=True)
class AgentStatusOverlay:
    """Bounded, model-only status with audit-safe metadata."""

    content: str
    status_revision: int
    source_event_ids: tuple[str, ...]
    generated_at: str
    scope: str = "logical_request"

    def audit_metadata(self, result: OverlayResult) -> dict[str, Any]:
        return {
            "status_revision": self.status_revision,
            "status_schema_version": STATUS_SCHEMA_VERSION,
            "source_event_ids": list(self.source_event_ids),
            "generated_at": self.generated_at,
            "scope": self.scope,
            "overlay_result": result,
        }


def begin_logical_user_request(
    message_metadata: MutableMapping[str, Any],
    session_metadata: MutableMapping[str, Any],
) -> str:
    """Atomically replace a session ledger for a real inbound user request."""
    request_id = new_audit_id()
    message_metadata[LOGICAL_USER_REQUEST_ID_META] = request_id
    session_metadata[AGENT_STATUS_FAILURE_LEDGER_KEY] = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "logical_user_request_id": request_id,
        "revision": 0,
        "failures": {},
    }
    return request_id


def inherit_logical_user_request(
    message_metadata: Mapping[str, Any] | None,
    session_metadata: Mapping[str, Any] | None,
) -> str | None:
    """Return a continuation's request id only when it matches persisted state."""
    request_id = (
        message_metadata.get(LOGICAL_USER_REQUEST_ID_META)
        if isinstance(message_metadata, Mapping)
        else None
    )
    ledger = _validated_ledger(session_metadata)
    if ledger is None:
        return None
    if request_id is None:
        return ledger["logical_user_request_id"]
    if not isinstance(request_id, str) or not request_id:
        return None
    return request_id if ledger["logical_user_request_id"] == request_id else None


def record_tool_terminal(
    session_metadata: MutableMapping[str, Any],
    message_metadata: Mapping[str, Any] | None,
    *,
    tool_name: str,
    tool: Any,
    params: Any,
    outcome: ToolAuditOutcome,
) -> bool:
    """Persist one terminal operation outcome in the active logical request.

    A missing or unsafe fingerprint is intentionally ignored: counting it
    would create an imprecise retry signal.  Cancellation is retained as an
    interrupted outcome but never counted as a retryable failure.
    """
    request_id = inherit_logical_user_request(message_metadata, session_metadata)
    if request_id is None:
        return False
    evidence = tool_operation_evidence(tool_name, tool, params)
    if not evidence.retry_key:
        return False
    parsed = _validated_ledger(session_metadata)
    if parsed is None:
        return False
    ledger = parsed
    failures = ledger["failures"]
    previous = failures.get(evidence.retry_key)
    source_event_id = outcome.source_event_id
    status = _terminal_status(outcome)

    if status == "ok":
        failures.pop(evidence.retry_key, None)
    else:
        old_count = _non_negative_int((previous or {}).get("consecutive_failures"))
        failures[evidence.retry_key] = {
            "tool_name": str(tool_name)[:80],
            "consecutive_failures": old_count + 1 if status in {"error", "blocked", "timeout"} else 0,
            "last_error_class": _error_class(outcome),
            "last_outcome": status,
            "source_event_ids": _append_source_event_id(
                (previous or {}).get("source_event_ids"), source_event_id
            ),
            "updated_at": _utc_now(),
        }
    ledger["revision"] = _non_negative_int(ledger.get("revision")) + 1
    session_metadata[AGENT_STATUS_FAILURE_LEDGER_KEY] = ledger
    return True


def build_status_overlay(
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None,
    *,
    active_goal: Mapping[str, Any] | None = None,
    owner_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> AgentStatusOverlay | None:
    """Render one bounded model-only projection from durable request and Goal facts.

    ``active_goal`` and ``owner_records`` are snapshots supplied by
    :class:`GoalOrchestrationStore`.  They are intentionally read-only; task
    lifecycle, delivery, and completion remain owned by their existing paths.
    """
    lines = ["[Agent Status]"]
    scopes: list[str] = []
    source_event_ids: list[str] = []
    revision_facts: dict[str, Any] = {}
    failure_lines, failure_sources, failure_revision = _repeated_failure_lines(
        session_metadata,
        message_metadata,
    )
    if failure_lines:
        lines.extend(failure_lines)
        scopes.append("logical_request")
        source_event_ids.extend(failure_sources)
        revision_facts["logical_request"] = {
            "ledger_revision": failure_revision,
            "sources": failure_sources,
            "lines": failure_lines,
        }

    owner_projection = _obligation_projection(owner_records)
    if owner_projection is not None:
        lines.append(_render_owner_run_line(owner_projection))
        scopes.append("owner_run")
        revision_facts["owner_run"] = owner_projection

    goal_projection = _goal_projection(active_goal)
    if goal_projection is not None:
        lines.append(_render_active_goal_line(goal_projection))
        scopes.append("active_goal")
        revision_facts["active_goal"] = goal_projection

    if not scopes:
        return None
    lines.append("[/Agent Status]")
    content = "\n".join(lines)
    if len(content) > 1000:
        return None
    return AgentStatusOverlay(
        content=content,
        status_revision=_snapshot_revision(revision_facts),
        source_event_ids=tuple(_unique_source_event_ids(source_event_ids)),
        generated_at=_utc_now(),
        scope="+".join(scopes),
    )


def _repeated_failure_lines(
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None,
) -> tuple[list[str], list[str], int]:
    """Return repeated-failure display facts without source parameters."""
    if inherit_logical_user_request(message_metadata, session_metadata) is None:
        return [], [], 0
    ledger = _validated_ledger(session_metadata)
    if ledger is None:
        return [], [], 0
    failures: list[tuple[str, int, str, Mapping[str, Any]]] = []
    source_event_ids: list[str] = []
    for entry in ledger["failures"].values():
        if not isinstance(entry, Mapping):
            continue
        count = _non_negative_int(entry.get("consecutive_failures"))
        outcome = entry.get("last_outcome")
        name = entry.get("tool_name")
        if count < 2 or outcome not in {"error", "blocked", "timeout"} or not isinstance(name, str):
            continue
        failures.append((name, count, _safe_error_class(entry.get("last_error_class")), entry))
    if not failures:
        return [], [], _non_negative_int(ledger.get("revision"))
    failures.sort(key=lambda item: (-item[1], item[0]))
    lines = []
    for name, count, error_class, entry in failures[:_MAX_RENDERED_FAILURES]:
        lines.append(
            f"Repeated failure: {name} same-operation failures={count}; class={error_class}"
        )
        for event_id in entry.get("source_event_ids", []):
            if isinstance(event_id, str) and event_id and event_id not in source_event_ids:
                source_event_ids.append(event_id)
    return lines, source_event_ids, _non_negative_int(ledger.get("revision"))


def _goal_projection(active_goal: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(active_goal, Mapping) or active_goal.get("status") != "active":
        return None
    orchestration = active_goal.get("orchestration")
    tasks = orchestration.get("tasks") if isinstance(orchestration, Mapping) else None
    return _obligation_projection(tasks if isinstance(tasks, Mapping) else {}) or {
        "counts": _empty_status_counts(),
        "completion": "ready",
        "delivery_pending": 0,
    }


def _obligation_projection(
    records: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    if not isinstance(records, Mapping):
        return None
    tasks = {
        str(task_id): dict(record)
        for task_id, record in records.items()
        if isinstance(record, Mapping) and record.get("required") is True
    }
    if not tasks:
        return None
    roots = _root_obligation_ids(tasks)
    counts = _empty_status_counts()
    delivery_pending = 0
    for task_id in roots:
        status, chain = _resolved_obligation_status(tasks, task_id)
        counts[status] = counts.get(status, 0) + 1
        leaf = tasks.get(chain[-1]) if chain else None
        result = leaf.get("result") if isinstance(leaf, Mapping) else None
        if isinstance(result, Mapping) and result.get("available") is True:
            phase = str(result.get("delivery_phase") or "unclaimed")
            if phase != "delivered":
                delivery_pending += 1
    return {
        "counts": counts,
        "completion": "ready" if counts["succeeded"] == len(roots) else "blocked",
        "delivery_pending": delivery_pending,
    }


def _root_obligation_ids(tasks: Mapping[str, Mapping[str, Any]]) -> list[str]:
    replaced = {
        record.get("resolved_by_task_id")
        for record in tasks.values()
        if isinstance(record.get("resolved_by_task_id"), str)
        and record.get("resolved_by_task_id") in tasks
    }
    return sorted(task_id for task_id in tasks if task_id not in replaced)


def _resolved_obligation_status(
    tasks: Mapping[str, Mapping[str, Any]],
    task_id: str,
) -> tuple[str, list[str]]:
    chain: list[str] = []
    seen: set[str] = set()
    current = task_id
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        record = tasks.get(current)
        if not isinstance(record, Mapping):
            return "lost", chain
        status = str(record.get("status") or "lost")
        if status == "succeeded":
            return "succeeded", chain
        replacement = record.get("resolved_by_task_id")
        if isinstance(replacement, str) and replacement:
            current = replacement
            continue
        return status if status in _STATUS_ORDER else "lost", chain
    return "lost", chain


_STATUS_ORDER = ("succeeded", "running", "failed", "cancelled", "timed_out", "lost")


def _empty_status_counts() -> dict[str, int]:
    return {status: 0 for status in _STATUS_ORDER}


def _render_owner_run_line(projection: Mapping[str, Any]) -> str:
    return (
        "Owner Run required: "
        f"{_render_counts(projection.get('counts'))}; "
        f"completion={projection.get('completion', 'blocked')}; "
        f"delivery_pending={_non_negative_int(projection.get('delivery_pending'))}"
    )


def _render_active_goal_line(projection: Mapping[str, Any]) -> str:
    return (
        "Active Goal required: "
        f"{_render_counts(projection.get('counts'))}; "
        f"completion={projection.get('completion', 'blocked')}; "
        f"delivery_pending={_non_negative_int(projection.get('delivery_pending'))}"
    )


def _render_counts(value: Any) -> str:
    counts = value if isinstance(value, Mapping) else {}
    fields = [f"{status}={_non_negative_int(counts.get(status))}" for status in _STATUS_ORDER[:3]]
    fields.extend(
        f"{status}={_non_negative_int(counts.get(status))}"
        for status in _STATUS_ORDER[3:]
        if _non_negative_int(counts.get(status))
    )
    return " ".join(fields)


def _snapshot_revision(facts: Mapping[str, Any]) -> int:
    """Return a deterministic revision for one already-committed fact snapshot."""
    raw = json.dumps(facts, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16)


def _unique_source_event_ids(value: list[str]) -> list[str]:
    ids: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in ids:
            ids.append(item)
    return ids[:_MAX_SOURCE_EVENT_IDS]


class FailureLedgerHook(AgentHook):
    """Persist the ledger after a terminal tool audit event and before checkpointing."""

    def __init__(
        self,
        *,
        session: Session,
        sessions: SessionManager,
    ) -> None:
        super().__init__()
        self._session = session
        self._sessions = sessions

    async def after_execute_tool_terminal(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
        outcome: ToolAuditOutcome,
    ) -> None:
        if record_tool_terminal(
            self._session.metadata,
            None,
            tool_name=str(getattr(tool_call, "name", "")),
            tool=tool,
            params=params,
            outcome=outcome,
        ):
            self._sessions.save(self._session)


def _validated_ledger(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(AGENT_STATUS_FAILURE_LEDGER_KEY)
    if not isinstance(raw, Mapping):
        return None
    request_id = raw.get("logical_user_request_id")
    failures = raw.get("failures")
    if raw.get("schema_version") != STATUS_SCHEMA_VERSION or not isinstance(request_id, str):
        return None
    if not isinstance(failures, Mapping):
        return None
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "logical_user_request_id": request_id,
        "revision": _non_negative_int(raw.get("revision")),
        "failures": {str(key): dict(value) for key, value in failures.items() if isinstance(value, Mapping)},
    }


def _terminal_status(outcome: ToolAuditOutcome) -> str:
    if outcome.status == "ok":
        return "ok"
    if outcome.status == "cancelled":
        return "interrupted"
    if outcome.status == "error" and (
        outcome.error_type in {"Timeout", "TimeoutError"}
        or outcome.error_kind in {"Timeout", "TimeoutError"}
    ):
        return "timeout"
    return outcome.status if outcome.status in {"error", "blocked", "timeout"} else "unknown"


def _error_class(outcome: ToolAuditOutcome) -> str:
    failure = outcome.failure
    return _safe_error_class(
        failure.error_code if failure and failure.error_code else (
            failure.error_type if failure else outcome.error_code or outcome.error_type or outcome.error_kind
        )
    )


def _safe_error_class(value: Any) -> str:
    text = str(value or "unknown")
    return "".join(char for char in text if char.isalnum() or char in {"_", "-"})[:80] or "unknown"


def _append_source_event_id(value: Any, source_event_id: str | None) -> list[str]:
    ids = [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []
    if source_event_id and source_event_id not in ids:
        ids.append(source_event_id)
    return ids[-_MAX_SOURCE_EVENT_IDS:]


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
