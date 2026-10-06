"""M18 task and tool-step evidence with fail-closed risk classification.

The old Phase 6 selector receives one set of tools for a whole turn.  M18
keeps that selector intact while adding a separate, redacted evidence stream:
one task envelope plus one row for every ordered tool operation.  The module
never stores raw arguments, command text, file contents, credentials or
provider payloads.  It is safe to run in shadow mode beside the old gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from nanobot.memory.semantic_evidence import SemanticProjection, project_user_task

RISK_LEVELS = ("R0", "R1", "R2", "R3", "R4")
QUALIFICATIONS = (
    "candidate_eligible",
    "partial_evidence",
    "manual_review_required",
    "unsafe_for_skill",
)

_READ_ONLY_TOOLS = frozenset(
    {"read_file", "list_dir", "find_files", "grep", "skill_catalog_search", "skill_read"}
)
_EXTERNAL_READ_TOOLS = frozenset({"web_search", "web_fetch"})
_CONTROLLED_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
_SIDE_EFFECT_TOOLS = frozenset(
    {
        "exec", "write_stdin", "run_cli_app", "message", "cron", "spawn", "await_subagents",
        "create_goal", "update_goal", "generate_image",
    }
)
_SENSITIVE_MARKERS = re.compile(
    r"(?i)(?:secret|token|password|credential|private[_ -]?key|api[_ -]?key|authorization)"
)
_BOUNDARY_MARKERS = re.compile(
    r"(?i)(?:outside[_ -]?(?:workspace|allowed)|traversal|越界|破坏性|不可逆|特权|destructive|privileged)"
)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class StepEvidence:
    sequence_no: int
    trace_id: str | None
    event_id: str | None
    tool_name: str
    operation_kind: str
    input_summary: str | None
    output_summary: str | None
    status: str
    failure_family: str | None
    side_effect_class: str
    risk_level: str
    resource_key: str | None
    verification_kind: str | None
    candidate_use: str
    redaction_status: str = "safe"


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    task_id: str
    workspace: str
    session_key: str | None
    source_trace_ids: tuple[str, ...]
    projection: SemanticProjection | None
    actual_outcome: str
    verification_status: str
    qualification: str
    legacy_qualification: str
    rejection_reason: str | None
    steps: tuple[StepEvidence, ...]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _compact(value: Any, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = _SPACE_RE.sub(" ", str(value)).strip()
    return text[:limit] if text else None


def _safe_summary(value: Any, limit: int = 240) -> str | None:
    """Keep only bounded diagnostic labels, never an untrusted raw payload."""

    text = _compact(value, limit)
    if not text:
        return None
    if _SENSITIVE_MARKERS.search(text):
        return "[REDACTED:SENSITIVE]"
    if _BOUNDARY_MARKERS.search(text):
        return "[REDACTED:BOUNDARY]"
    return text


def _failure_family(event: Mapping[str, Any]) -> str | None:
    value = " ".join(
        str(event.get(key) or "")
        for key in ("error_code", "error_type", "error_source", "failure_family", "detail")
    ).casefold()
    if not value:
        return None
    if "file_not_found" in value or "filenotfound" in value or "not found" in value:
        return "resource_not_found"
    if "permission" in value or "access_denied" in value or "forbidden" in value:
        return "permission_denied"
    if "invalid" in value or "validation" in value:
        return "invalid_arguments"
    if "timeout" in value:
        return "timeout"
    if "rate" in value or "429" in value:
        return "rate_limited"
    if "policy" in value or "blocked" in value or "boundary" in value:
        return "policy_blocked"
    return "tool_exception"


def _side_effect_class(event: Mapping[str, Any], tool_name: str) -> str:
    explicit = str(event.get("side_effect_class") or "").strip().lower()
    if explicit in {"filesystem_write", "process_execution", "external_message", "scheduler", "subagent", "sensitive", "none"}:
        return explicit
    raw = event.get("side_effects")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        kinds = {str(item.get("kind")) for item in raw if isinstance(item, Mapping)}
        if "filesystem_path" in kinds:
            return "filesystem_write" if tool_name in _CONTROLLED_WRITE_TOOLS else "filesystem_read"
        if "process_execution" in kinds:
            return "process_execution"
    if tool_name in _CONTROLLED_WRITE_TOOLS:
        return "filesystem_write"
    if tool_name in {"message"}:
        return "external_message"
    if tool_name in {"cron"}:
        return "scheduler"
    if tool_name in {"spawn", "await_subagents"}:
        return "subagent"
    if tool_name in {"exec", "write_stdin", "run_cli_app"}:
        return "process_execution"
    return "none"


def classify_risk(
    tool_name: str,
    *,
    side_effect_class: str = "none",
    event: Mapping[str, Any] | None = None,
) -> str:
    """Return the highest risk implied by capability, target and effect."""

    name = str(tool_name or "").strip()
    payload = event or {}
    labels = " ".join(str(payload.get(key) or "") for key in ("safe_input_summary", "detail", "error_summary"))
    if _SENSITIVE_MARKERS.search(labels) or _BOUNDARY_MARKERS.search(labels):
        return "R4"
    if side_effect_class in {"sensitive"}:
        return "R4"
    if side_effect_class in {"process_execution", "external_message", "scheduler", "subagent"}:
        return "R3"
    if side_effect_class == "filesystem_write" or name in _CONTROLLED_WRITE_TOOLS:
        return "R2"
    if name in _SIDE_EFFECT_TOOLS:
        return "R3"
    if name in _EXTERNAL_READ_TOOLS:
        return "R1"
    if name in _READ_ONLY_TOOLS:
        return "R0"
    return "R3"


def _operation_kind(event: Mapping[str, Any], tool_name: str) -> str:
    return _safe_summary(event.get("operation_kind"), 80) or {
        "read_file": "file_read", "list_dir": "directory_list", "find_files": "file_search",
        "grep": "text_search", "skill_catalog_search": "skill_search", "skill_read": "skill_read",
        "web_search": "external_search", "web_fetch": "external_fetch",
        "write_file": "file_write", "edit_file": "file_edit", "apply_patch": "file_patch",
        "exec": "process_execute", "write_stdin": "process_continue", "message": "external_message",
        "cron": "schedule_change", "spawn": "subagent_spawn",
    }.get(tool_name, "unknown")


def _candidate_use(risk: str, status: str, verification_kind: str | None) -> str:
    if risk == "R4":
        return "archive_only"
    if status not in {"ok", "success"}:
        return "unverified"
    if risk in {"R2", "R3"}:
        return "abstract_only"
    return "reusable" if verification_kind else "unverified"


def build_step_evidence(
    events: Iterable[Mapping[str, Any]],
    *,
    trace_id: str | None = None,
) -> tuple[StepEvidence, ...]:
    steps: list[StepEvidence] = []
    for sequence_no, raw in enumerate(events):
        event = raw if isinstance(raw, Mapping) else {}
        tool_name = str(event.get("tool_name") or event.get("name") or "").strip()
        if not tool_name:
            # Missing tool identity is evidence failure, not an invented step.
            steps.append(StepEvidence(sequence_no, trace_id, None, "unknown", "unknown", None, None,
                                      "unverified", "unknown_operation", "none", "R4", None, None,
                                      "unverified", "rejected"))
            continue
        effect = _side_effect_class(event, tool_name)
        risk = classify_risk(tool_name, side_effect_class=effect, event=event)
        status = _safe_summary(event.get("status"), 32) or "unverified"
        verification = _safe_summary(event.get("verification_kind"), 80)
        if event.get("verification_kind") is None and status in {"ok", "success"}:
            verification = "tool_success"
        steps.append(StepEvidence(
            sequence_no=sequence_no,
            trace_id=str(event.get("trace_id") or trace_id or "") or None,
            event_id=str(event.get("event_id") or "") or None,
            tool_name=tool_name[:80],
            operation_kind=_operation_kind(event, tool_name),
            input_summary=_safe_summary(event.get("safe_input_summary") or event.get("input_summary")),
            output_summary=_safe_summary(event.get("output_summary") or event.get("safe_output_summary")),
            status=status,
            failure_family=_failure_family(event),
            side_effect_class=effect,
            risk_level=risk,
            resource_key=_safe_summary(event.get("resource_key"), 180),
            verification_kind=verification,
            candidate_use=_candidate_use(risk, status, verification),
        ))
    return tuple(steps)


def _qualification(
    steps: Sequence[StepEvidence],
    *,
    projection: SemanticProjection | None,
    actual_outcome: str,
    mixed_risk_policy: str,
) -> tuple[str, str | None]:
    if projection is None:
        return "manual_review_required", "task_goal_or_redaction_unavailable"
    if not steps:
        return "manual_review_required", "no_verified_steps"
    if any(step.redaction_status != "safe" for step in steps):
        return "manual_review_required", "step_redaction_failed"
    if any(step.risk_level == "R4" for step in steps):
        return "unsafe_for_skill", "r4_sensitive_or_boundary_operation"
    high_risk = any(step.risk_level in {"R2", "R3"} for step in steps)
    failed = any(step.status not in {"ok", "success"} for step in steps)
    if high_risk:
        if mixed_risk_policy == "reject":
            return "manual_review_required", "mixed_risk_policy_reject"
        if mixed_risk_policy == "partial":
            return "partial_evidence", "high_risk_steps_require_isolation"
        return "manual_review_required", "high_risk_steps_require_manual_review"
    if failed and actual_outcome not in {"success", "completed", "ok", "done"}:
        return "manual_review_required", "unverified_failed_task"
    if failed:
        return "partial_evidence", "failed_step_requires_recovery_review"
    return "candidate_eligible", None


def build_task_evidence(
    *,
    workspace: str,
    session_key: str | None,
    trace_id: str,
    turn_id: str | None,
    user_text: str,
    events: Iterable[Mapping[str, Any]],
    actual_outcome: str,
    legacy_qualification: str = "unknown",
    mixed_risk_policy: str = "manual",
    task_id: str | None = None,
) -> TaskEvidence:
    event_list = tuple(events)
    names = tuple(str(item.get("tool_name") or item.get("name") or "") for item in event_list if isinstance(item, Mapping))
    projection, _reason = project_user_task(user_text, names)
    steps = build_step_evidence(event_list, trace_id=trace_id)
    qualification, rejection_reason = _qualification(
        steps, projection=projection, actual_outcome=actual_outcome,
        mixed_risk_policy=mixed_risk_policy,
    )
    verification = "verified" if steps and all(step.status in {"ok", "success"} for step in steps) else (
        "partially_verified" if steps else "unverified"
    )
    stable = task_id or f"evolution-task:{trace_id}"
    return TaskEvidence(
        task_id=stable,
        workspace=workspace,
        session_key=session_key,
        source_trace_ids=(trace_id,),
        projection=projection,
        actual_outcome=_safe_summary(actual_outcome, 80) or "unknown",
        verification_status=verification,
        qualification=qualification,
        legacy_qualification=legacy_qualification[:80],
        rejection_reason=rejection_reason,
        steps=steps,
    )


def _projection_values(task: TaskEvidence) -> tuple[str | None, str | None, str | None, str | None, str]:
    projection = task.projection
    return (
        projection.task_goal if projection else None,
        projection.intent if projection else None,
        projection.input_scope if projection else None,
        projection.expected_outcome if projection else None,
        projection.semantic_key if projection else f"task:{hashlib.sha256(task.task_id.encode()).hexdigest()[:24]}",
    )


def persist_task_evidence(connection: sqlite3.Connection, task: TaskEvidence) -> TaskEvidence:
    """Upsert one task and its ordered steps; retries are idempotent."""

    goal, intent, scope, expected, _semantic_key = _projection_values(task)
    now = _now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """INSERT INTO evolution_task_evidence
               (task_id,workspace,session_key,source_trace_ids_json,goal,intent,scope,expected_outcome,
                actual_outcome,verification_status,qualification,legacy_qualification,redaction_status,
                schema_version,rejection_reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(task_id) DO UPDATE SET workspace=excluded.workspace,session_key=excluded.session_key,
                source_trace_ids_json=excluded.source_trace_ids_json,goal=excluded.goal,intent=excluded.intent,
                scope=excluded.scope,expected_outcome=excluded.expected_outcome,actual_outcome=excluded.actual_outcome,
                verification_status=excluded.verification_status,qualification=excluded.qualification,
                legacy_qualification=excluded.legacy_qualification,redaction_status=excluded.redaction_status,
                schema_version=excluded.schema_version,rejection_reason=excluded.rejection_reason,updated_at=excluded.updated_at""",
            (task.task_id, task.workspace, task.session_key, json.dumps(task.source_trace_ids), goal, intent, scope,
             expected, task.actual_outcome, task.verification_status, task.qualification,
             task.legacy_qualification, "safe" if all(item.redaction_status == "safe" for item in task.steps) else "rejected",
             "m18-v1", task.rejection_reason, now, now),
        )
        connection.execute("DELETE FROM evolution_step_evidence WHERE task_id=?", (task.task_id,))
        connection.executemany(
            """INSERT INTO evolution_step_evidence
               (step_id,task_id,workspace,sequence_no,trace_id,event_id,tool_name,operation_kind,input_summary,
                output_summary,status,failure_family,side_effect_class,risk_level,resource_key,
                verification_kind,candidate_use,redaction_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (f"{task.task_id}:step:{step.sequence_no}", task.task_id, task.workspace, step.sequence_no, step.trace_id,
                 step.event_id, step.tool_name, step.operation_kind, step.input_summary, step.output_summary,
                 step.status, step.failure_family, step.side_effect_class, step.risk_level, step.resource_key,
                 step.verification_kind, step.candidate_use, step.redaction_status, now)
                for step in task.steps
            ],
        )
        for source_id in task.source_trace_ids:
            connection.execute(
                """INSERT OR IGNORE INTO evolution_task_links
                   (link_id,task_id,source_type,source_id,relation,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (f"evolution-link:{uuid4()}", task.task_id, "trace", source_id, "source", now),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return task


def load_stepwise_candidate_records(
    connection: sqlite3.Connection,
    workspace: str,
    *,
    include_partial: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Expose only safe M18 records to the M19 selector."""

    statuses = ("candidate_eligible", "partial_evidence") if include_partial else ("candidate_eligible",)
    rows = connection.execute(
        """SELECT task_id,source_trace_ids_json,goal,intent,scope,expected_outcome,qualification
           FROM evolution_task_evidence
           WHERE workspace=? AND redaction_status='safe' AND qualification IN (?,?)
           ORDER BY created_at""",
        (workspace, statuses[0], statuses[1] if len(statuses) > 1 else statuses[0]),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            traces = tuple(str(item) for item in json.loads(row[1] or "[]") if str(item))
        except (TypeError, ValueError, json.JSONDecodeError):
            traces = ()
        steps = connection.execute(
            """SELECT tool_name,risk_level,candidate_use FROM evolution_step_evidence
               WHERE task_id=? ORDER BY sequence_no""", (row[0],)
        ).fetchall()
        tools = tuple(str(item[0]) for item in steps if item[2] in {"reusable", "abstract_only"})
        if not traces or not row[2] or not tools:
            continue
        semantic_source = "|".join((str(row[3] or ""), str(row[2] or "").casefold(), ",".join(sorted(tools))))
        task_key = "semantic:" + hashlib.sha256(semantic_source.encode("utf-8")).hexdigest()[:24]
        records.append({
            "evidence_id": f"evolution:{row[0]}", "trace_id": traces[0], "summary": str(row[2]),
            "intent": str(row[3] or ""), "input_scope": str(row[4] or ""),
            "expected_outcome": str(row[5] or ""), "tools": tools, "task_key": task_key,
            "outcome": "success", "semantic": True, "stepwise": True,
            "qualification": str(row[6]), "source_task_id": str(row[0]),
        })
    return tuple(records)


def load_task_evidence(connection: sqlite3.Connection, task_id: str) -> TaskEvidence | None:
    row = connection.execute(
        """SELECT task_id,workspace,session_key,source_trace_ids_json,goal,intent,scope,expected_outcome,
                  actual_outcome,verification_status,qualification,legacy_qualification,rejection_reason
           FROM evolution_task_evidence WHERE task_id=?""", (task_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        traces = tuple(str(item) for item in json.loads(row[3] or "[]") if str(item))
    except (TypeError, ValueError, json.JSONDecodeError):
        traces = ()
    steps = tuple(
        StepEvidence(
            int(item[0]), item[1], item[2], str(item[3]), str(item[4]), item[5], item[6], str(item[7]),
            item[8], str(item[9]), str(item[10]), item[11], item[12], str(item[13]), str(item[14]),
        )
        for item in connection.execute(
            """SELECT sequence_no,trace_id,event_id,tool_name,operation_kind,input_summary,output_summary,
                      status,failure_family,side_effect_class,risk_level,resource_key,verification_kind,
                      candidate_use,redaction_status FROM evolution_step_evidence
               WHERE task_id=? ORDER BY sequence_no""", (task_id,)
        ).fetchall()
    )
    projection = None
    if row[4]:
        projection = SemanticProjection(str(row[4]), str(row[5] or ""), str(row[6] or ""),
                                         str(row[7] or ""), tuple(step.tool_name for step in steps),
                                         "semantic:" + hashlib.sha256(str(row[4]).encode("utf-8")).hexdigest()[:24])
    return TaskEvidence(
        task_id=str(row[0]), workspace=str(row[1]), session_key=str(row[2]) if row[2] else None,
        source_trace_ids=traces, projection=projection, actual_outcome=str(row[8] or "unknown"),
        verification_status=str(row[9]), qualification=str(row[10]),
        legacy_qualification=str(row[11]), rejection_reason=row[12], steps=steps,
    )


__all__ = [
    "QUALIFICATIONS", "RISK_LEVELS", "StepEvidence", "TaskEvidence", "build_step_evidence",
    "build_task_evidence", "classify_risk", "load_stepwise_candidate_records",
    "load_task_evidence", "persist_task_evidence",
]
