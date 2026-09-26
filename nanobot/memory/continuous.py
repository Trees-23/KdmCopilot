"""Phase 6 controlled continuous-review primitives.

Phase 6 is intentionally an opt-in orchestration layer.  It only produces
derived proposals and evidence; it never publishes a Skill, merges Git, or
deletes a user's memory.  The default configuration is disabled and every
write path checks the kill switch before doing work.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from nanobot.memory.derivation import CaseCandidate, classify_outcome, derive_case_candidate

PHASE6_DIR = ".nanobot/phase6"
EVIDENCE_FILE = "evidence.jsonl"
STATE_FILE = "state.json"
READ_ONLY_TOOLS = frozenset({"read_file", "list_dir", "skill_catalog_search", "skill_read"})
SENSITIVE_MARKERS = frozenset({"secret", "token", "password", "api key", "credential", "private key"})
_SECRET_RE = re.compile(r"(?i)(?:api[_ -]?key|token|password|secret)\s*[:=]\s*[^\s,;]+")
_SPACE_RE = re.compile(r"\s+")


class Phase6DisabledError(RuntimeError):
    """Raised when an opt-in Phase 6 write is attempted while disabled."""


Phase6Disabled = Phase6DisabledError


@dataclass(frozen=True, slots=True)
class Phase6RuntimeConfig:
    enabled: bool = False
    kill_switch: bool = False
    min_repeat_count: int = 2
    max_candidates_per_cycle: int = 20
    regression_pause_threshold: int = 3
    max_memory_growth_ratio: float = 0.5
    retention_days: int = 18
    payload_retention_days: int = 7

    @classmethod
    def from_config(cls, config: Any) -> "Phase6RuntimeConfig":
        """Build the runtime DTO from ``Config.phase6`` or a mapping."""

        if isinstance(config, Mapping):
            values = config
        else:
            values = {name: getattr(config, name) for name in cls.__dataclass_fields__ if hasattr(config, name)}
        return cls(**values)

    def __post_init__(self) -> None:
        if self.min_repeat_count < 2:
            raise ValueError("min_repeat_count must be at least 2")
        if self.max_candidates_per_cycle < 1:
            raise ValueError("max_candidates_per_cycle must be positive")
        if self.regression_pause_threshold < 1:
            raise ValueError("regression_pause_threshold must be positive")
        if not 0 <= self.max_memory_growth_ratio:
            raise ValueError("max_memory_growth_ratio must not be negative")
        if self.payload_retention_days < 1 or self.retention_days < self.payload_retention_days:
            raise ValueError("retention_days must be >= payload_retention_days >= 1")


@dataclass(frozen=True, slots=True)
class TraceCandidate:
    task_key: str
    trace_ids: tuple[str, ...]
    summary: str
    frequency: int
    value_score: float


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    event: str
    occurred_at: str
    trace_ids: tuple[str, ...] = ()
    case_ids: tuple[str, ...] = ()
    eval_run_ids: tuple[str, ...] = ()
    status: str = "ok"
    metrics: Mapping[str, Any] | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Phase6State:
    paused: bool = False
    pause_reason: str | None = None
    consecutive_regressions: int = 0
    generation: int = 0


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    payload_expiry_trace_ids: tuple[str, ...]
    summary_review_trace_ids: tuple[str, ...]
    protected_summary_trace_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Phase6CycleResult:
    status: str
    selected_tasks: tuple[TraceCandidate, ...] = ()
    regression_case_ids: tuple[str, ...] = ()
    paused: bool = False
    rollback_applied: bool = False


@dataclass(frozen=True, slots=True)
class Phase6Proposal:
    proposal_id: str
    target: str
    trace_ids: tuple[str, ...]
    case_ids: tuple[str, ...]
    eval_run_ids: tuple[str, ...]
    status: str = "eligible_for_confirmation"


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _iso(value: datetime | None = None) -> str:
    return _now(value).isoformat(timespec="seconds")


def _config_value(config: Any, name: str, default: Any) -> Any:
    return getattr(config, name, default)


def phase6_active(config: Any) -> bool:
    """Return whether Phase 6 may process candidates right now."""

    return bool(_config_value(config, "enabled", False)) and not bool(
        _config_value(config, "kill_switch", False)
    )


def require_active(config: Any) -> None:
    if not phase6_active(config):
        raise Phase6DisabledError("phase6 is disabled or the kill switch is active")


def _normalize_task(summary: str) -> tuple[str, str]:
    clean = _SPACE_RE.sub(" ", _SECRET_RE.sub("[redacted]", summary)).strip()
    return clean.casefold(), clean[:500]


def _is_low_risk(record: Mapping[str, Any]) -> bool:
    outcome = classify_outcome(str(record.get("outcome") or record.get("status") or ""))
    if outcome != "success":
        return False
    sensitivity = str(record.get("sensitivity") or "").casefold()
    summary = str(record.get("summary") or "")
    if sensitivity in {"secret", "sensitive", "private", "restricted"}:
        return False
    lowered = summary.casefold()
    if any(marker in lowered for marker in SENSITIVE_MARKERS):
        return False
    tools = record.get("tools") or ()
    if not tools and int(record.get("tool_count") or 0) > 0:
        return False
    return all(str(tool) in READ_ONLY_TOOLS for tool in tools)


def select_low_risk_tasks(
    records: Iterable[Mapping[str, Any]], config: Any,
) -> tuple[TraceCandidate, ...]:
    """Select repeated, successful, read-only task summaries only."""

    require_active(config)
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        if not _is_low_risk(record):
            continue
        trace_id = str(record.get("trace_id") or "").strip()
        key, summary = _normalize_task(str(record.get("summary") or ""))
        if not trace_id or not key:
            continue
        group = groups.setdefault(key, {"summary": summary, "trace_ids": [], "score": 0.0})
        group["trace_ids"].append(trace_id)
        group["score"] = max(group["score"], float(record.get("value_score") or 0.0))
    minimum = int(_config_value(config, "min_repeat_count", 2))
    candidates = [
        TraceCandidate(key, tuple(sorted(set(value["trace_ids"]))), value["summary"],
                       len(set(value["trace_ids"])), value["score"])
        for key, value in groups.items() if len(set(value["trace_ids"])) >= minimum
    ]
    candidates.sort(key=lambda item: (-item.frequency, -item.value_score, item.task_key))
    return tuple(candidates[: int(_config_value(config, "max_candidates_per_cycle", 20))])


def load_trace_records(connection: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    """Read only redacted Trace index summaries for the selector."""

    rows = connection.execute(
        "SELECT trace_id,outcome,summary,event_count,tool_count FROM trace_index ORDER BY started_at"
    ).fetchall()
    return tuple({
        "trace_id": row[0], "outcome": row[1], "summary": row[2],
        "event_count": row[3], "tool_count": row[4],
    } for row in rows)


def load_failed_trace_records(connection: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    """Return failed/partial/blocked summaries without provider payloads."""

    rows = connection.execute(
        "SELECT trace_id,outcome,summary FROM trace_index "
        "WHERE outcome IN ('failure','failed','partial','blocked') ORDER BY started_at"
    ).fetchall()
    return tuple({"trace_id": row[0], "outcome": row[1], "summary": row[2]} for row in rows)


def regression_case_from_trace(
    connection: sqlite3.Connection,
    trace: Mapping[str, Any],
    config: Any,
    *,
    actor: str = "phase6-regression",
) -> CaseCandidate:
    """Convert a failed Trace summary into a staging Case, never a Skill."""

    require_active(config)
    trace_id = str(trace.get("trace_id") or "").strip()
    if not trace_id:
        raise ValueError("trace_id is required")
    classification = classify_outcome(str(trace.get("outcome") or trace.get("status") or ""))
    if classification == "success":
        raise ValueError("successful traces cannot become regression cases")
    summary = _SECRET_RE.sub("[redacted]", str(trace.get("summary") or "trace failure"))[:500]
    return derive_case_candidate(
        connection,
        trace_id=trace_id,
        intent=str(trace.get("intent") or "trace_regression"),
        task_signature={"source": "phase6", "classification": classification},
        outcome={"status": classification, "summary": summary, "actor": actor},
        failure_patterns=tuple(str(item) for item in (trace.get("failure_patterns") or ())) or (classification,),
        confidence=float(trace.get("confidence") or 0.7),
    )


def next_evalpack_version(connection: sqlite3.Connection, skill_revision_id: str) -> int:
    rows = connection.execute(
        "SELECT split_policy_json FROM eval_packs WHERE skill_revision_id=? ORDER BY created_at",
        (skill_revision_id,),
    ).fetchall()
    versions: list[int] = []
    for row in rows:
        try:
            versions.append(int(json.loads(row[0]).get("phase6_version", 0)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return max(versions, default=0) + 1


def build_versioned_evalpack(
    connection: sqlite3.Connection,
    *,
    skill_id: str,
    skill_revision_id: str,
    cases: Iterable[Mapping[str, Any]],
    fixture_hash: str,
    config: Any,
):
    """Build a new EvalPack version from changed regression cases."""

    require_active(config)
    from nanobot.memory.derivation import build_eval_pack_draft

    version = next_evalpack_version(connection, skill_revision_id)
    return build_eval_pack_draft(
        connection,
        skill_id=skill_id,
        skill_revision_id=skill_revision_id,
        cases=cases,
        fixture_hash=fixture_hash,
        split_policy={"seed": "stable-digest", "phase6_version": version},
        actor="phase6",
    )


def _phase6_dir(workspace: str | Path) -> Path:
    path = Path(workspace).expanduser().resolve() / PHASE6_DIR
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_RE.sub("[redacted]", value)[:2000]
    if isinstance(value, Mapping):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def append_evidence(workspace: str | Path, record: EvidenceRecord, config: Any) -> Path:
    """Append one payload-free, redacted evidence record and fsync it."""

    require_active(config)
    payload = _redact(asdict(record))
    payload["occurred_at"] = _iso(datetime.fromisoformat(record.occurred_at)) if record.occurred_at else _iso()
    path = _phase6_dir(workspace) / EVIDENCE_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        import os
        os.fsync(handle.fileno())
    return path


def load_state(workspace: str | Path) -> Phase6State:
    path = _phase6_dir(workspace) / STATE_FILE
    if not path.exists():
        return Phase6State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Phase6State(
            paused=bool(data.get("paused", False)), pause_reason=data.get("pause_reason"),
            consecutive_regressions=int(data.get("consecutive_regressions", 0)),
            generation=int(data.get("generation", 0)),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return Phase6State(paused=True, pause_reason="state_unreadable")


def save_state(workspace: str | Path, state: Phase6State) -> None:
    directory = _phase6_dir(workspace)
    target = directory / STATE_FILE
    with NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
        json.dump(asdict(state), handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        import os
        os.fsync(handle.fileno())
        temp = Path(handle.name)
    temp.replace(target)


def update_health(
    workspace: str | Path,
    metrics: Mapping[str, Any],
    config: Any,
) -> Phase6State:
    """Record health and pause candidate processing on unsafe signals."""

    require_active(config)
    previous = load_state(workspace)
    regressions = int(metrics.get("regression_failures", 0))
    reasons: list[str] = []
    if regressions >= int(_config_value(config, "regression_pause_threshold", 3)):
        reasons.append("regression_threshold")
    if int(metrics.get("permission_denials", 0)) > 0:
        reasons.append("permission_denial")
    if bool(metrics.get("fts_rebuild_failed", False)):
        reasons.append("fts_rebuild_failed")
    growth = float(metrics.get("memory_growth_ratio", 0.0))
    if growth > float(_config_value(config, "max_memory_growth_ratio", 0.5)):
        reasons.append("memory_capacity")
    state = Phase6State(
        paused=bool(reasons) or previous.paused,
        pause_reason=",".join(reasons) if reasons else previous.pause_reason,
        consecutive_regressions=regressions,
        generation=previous.generation + 1,
    )
    save_state(workspace, state)
    append_evidence(
        workspace,
        EvidenceRecord("health_check", _iso(), status="paused" if state.paused else "ok",
                       metrics=dict(metrics), reason=state.pause_reason),
        config,
    )
    return state


def collect_capacity_metrics(
    connection: sqlite3.Connection,
    *,
    baseline_memory_records: int = 0,
    max_memory_records: int = 100_000,
    max_retrieval_events: int = 200_000,
) -> dict[str, Any]:
    """Collect bounded counts without reading or deleting provider payloads."""

    memory_count = int(connection.execute("SELECT count(*) FROM memory_records").fetchone()[0])
    retrieval_count = int(connection.execute("SELECT count(*) FROM retrieval_events").fetchone()[0])
    trace_count = int(connection.execute("SELECT count(*) FROM trace_index").fetchone()[0])
    growth = 0.0
    if baseline_memory_records > 0:
        growth = (memory_count - baseline_memory_records) / baseline_memory_records
    return {
        "memory_records": memory_count,
        "retrieval_events": retrieval_count,
        "trace_index": trace_count,
        "memory_growth_ratio": growth,
        "capacity_exceeded": memory_count >= max_memory_records or retrieval_count >= max_retrieval_events,
    }


def record_index_failure(workspace: str | Path, error: str, config: Any) -> Phase6State:
    """Persist a redacted index failure and enter the normal pause path."""

    return update_health(workspace, {"fts_rebuild_failed": True, "error": error}, config)


def pause_and_rollback(
    connection: sqlite3.Connection,
    workspace: str | Path,
    metrics: Mapping[str, Any],
    config: Any,
    *,
    skill_id: str,
    baseline_revision_id: str,
    expected_current_revision_id: str | None,
    actor: str = "phase6-regression-guard",
) -> tuple[Phase6State, bool]:
    """Pause candidate processing and restore a known baseline with CAS."""

    state = update_health(workspace, metrics, config)
    rolled_back = False
    if state.paused:
        rolled_back = rollback_skill_revision(
            connection,
            skill_id=skill_id,
            target_revision_id=baseline_revision_id,
            expected_current_revision_id=expected_current_revision_id,
            config=config,
            actor=actor,
            confirm=True,
        )
        append_evidence(
            workspace,
            EvidenceRecord("rollback", _iso(), status="applied" if rolled_back else "skipped",
                           reason=state.pause_reason),
            config,
        )
    return state, rolled_back


def run_cycle(
    connection: sqlite3.Connection,
    workspace: str | Path,
    config: Any,
    *,
    trace_records: Iterable[Mapping[str, Any]],
    failed_traces: Iterable[Mapping[str, Any]] = (),
    metrics: Mapping[str, Any] | None = None,
) -> Phase6CycleResult:
    """Run one bounded opt-in cycle; no automatic adoption or publication."""

    if not phase6_active(config):
        return Phase6CycleResult("disabled")
    state = load_state(workspace)
    if state.paused:
        return Phase6CycleResult("paused", paused=True)
    if metrics:
        state = update_health(workspace, metrics, config)
        if state.paused:
            return Phase6CycleResult("paused", paused=True)
    selected = select_low_risk_tasks(trace_records, config)
    failed = tuple(failed_traces)
    case_ids: list[str] = []
    for trace in failed:
        case_ids.append(regression_case_from_trace(connection, trace, config).case_id)
    append_evidence(
        workspace,
        EvidenceRecord("cycle", _iso(),
                       trace_ids=tuple(str(item.get("trace_id")) for item in failed if item.get("trace_id")),
                       case_ids=tuple(case_ids), status="ok",
                       metrics={"selected_tasks": len(selected), "regression_cases": len(case_ids)}),
        config,
    )
    return Phase6CycleResult("completed", selected, tuple(case_ids))


def resume_after_review(workspace: str | Path, config: Any, *, actor: str, reason: str) -> Phase6State:
    require_active(config)
    if not actor.strip() or not reason.strip():
        raise ValueError("independent actor and reason are required")
    previous = load_state(workspace)
    state = Phase6State(generation=previous.generation + 1)
    save_state(workspace, state)
    append_evidence(workspace, EvidenceRecord("resume", _iso(), status="ok", reason=f"{actor}: {reason}"), config)
    return state


def plan_retention(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    payload_days: int = 7,
    trace_days: int = 18,
) -> RetentionPlan:
    """Plan expiry without deleting traces or long-lived summaries."""

    current = _now(now)
    payload_cutoff = _iso(current - timedelta(days=payload_days))
    trace_cutoff = _iso(current - timedelta(days=trace_days))
    rows = connection.execute(
        "SELECT trace_id,ended_at,summary FROM trace_index WHERE ended_at IS NOT NULL AND ended_at<=?",
        (payload_cutoff,),
    ).fetchall()
    payload = tuple(sorted(str(row[0]) for row in rows))
    old = connection.execute(
        "SELECT trace_id FROM trace_index WHERE ended_at IS NOT NULL AND ended_at<=?",
        (trace_cutoff,),
    ).fetchall()
    summary = tuple(sorted(str(row[0]) for row in old))
    return RetentionPlan(payload, summary, summary)


def make_proposal(
    *,
    source_kind: str,
    trace_ids: Sequence[str],
    case_ids: Sequence[str],
    eval_run_ids: Sequence[str],
    config: Any,
) -> Phase6Proposal:
    """Create a traceable proposal; the returned object is not an adoption."""

    require_active(config)
    if not trace_ids or not case_ids or not eval_run_ids:
        raise ValueError("proposal requires trace, case and eval run evidence")
    target = "git_pr_proposal" if source_kind in {"builtin", "shared", "entrypoint", "mcp"} else "workspace_adopt_proposal"
    return Phase6Proposal(f"phase6-proposal:{uuid4()}", target, tuple(trace_ids), tuple(case_ids), tuple(eval_run_ids))


def persist_proposal(
    connection: sqlite3.Connection,
    proposal: Phase6Proposal,
    *,
    workspace: str,
    skill_name: str,
    source_kind: str,
    baseline_hash: str,
    candidate_hash: str,
    baseline_revision_id: str | None = None,
    candidate_revision_id: str | None = None,
    skill_id: str | None = None,
    gate_snapshot: Mapping[str, Any] | None = None,
) -> Phase6Proposal:
    """Persist a generated Proposal without changing any current Skill pointer."""

    from nanobot.memory.proposal_repository import ProposalRepository

    ProposalRepository(connection).create_proposal(
        proposal_id=proposal.proposal_id,
        workspace=workspace,
        skill_name=skill_name,
        source_kind=source_kind,
        target=proposal.target,
        baseline_hash=baseline_hash,
        candidate_hash=candidate_hash,
        baseline_revision_id=baseline_revision_id,
        candidate_revision_id=candidate_revision_id,
        skill_id=skill_id,
        trace_ids=proposal.trace_ids,
        case_ids=proposal.case_ids,
        eval_run_ids=proposal.eval_run_ids,
        gate_snapshot=gate_snapshot,
        status="eligible_for_confirmation",
    )
    return proposal


def rollback_skill_revision(
    connection: sqlite3.Connection,
    *,
    skill_id: str,
    target_revision_id: str,
    expected_current_revision_id: str | None,
    config: Any,
    actor: str,
    confirm: bool = False,
) -> bool:
    """Atomically restore an existing revision after explicit confirmation."""

    require_active(config)
    if not actor.strip():
        raise ValueError("rollback actor is required")
    if not confirm:
        return False
    row = connection.execute(
        "SELECT skill_id,skill_version FROM skill_revisions WHERE revision_id=?",
        (target_revision_id,),
    ).fetchone()
    if row is None or row[0] != skill_id:
        raise ValueError("target revision does not belong to skill")
    current = connection.execute("SELECT current_revision_id FROM skills WHERE skill_id=?", (skill_id,)).fetchone()
    if current is None or current[0] != expected_current_revision_id:
        return False
    cursor = connection.execute(
        "UPDATE skills SET current_revision_id=?,current_version=?,updated_at=? "
        "WHERE skill_id=? AND current_revision_id=?",
        (target_revision_id, row[1], _iso(), skill_id, expected_current_revision_id),
    )
    connection.commit()
    return cursor.rowcount == 1


__all__ = [
    "EvidenceRecord", "Phase6CycleResult", "Phase6Disabled", "Phase6DisabledError", "Phase6Proposal",
    "Phase6RuntimeConfig", "Phase6State",
    "RetentionPlan", "TraceCandidate", "append_evidence", "build_versioned_evalpack",
    "load_failed_trace_records", "load_state", "load_trace_records", "make_proposal", "persist_proposal",
    "next_evalpack_version", "phase6_active", "plan_retention",
    "record_index_failure", "regression_case_from_trace", "require_active", "resume_after_review",
    "rollback_skill_revision", "run_cycle", "save_state", "select_low_risk_tasks", "update_health",
    "collect_capacity_metrics", "pause_and_rollback",
]
