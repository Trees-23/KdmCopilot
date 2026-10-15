"""Ordered end-of-turn evidence capture at the memory subsystem boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from nanobot.memory.asset_projection import project_task_asset
from nanobot.memory.recovery_evidence import link_successful_correction, record_failed_episode
from nanobot.memory.semantic_evidence import persist_turn_semantic_evidence
from nanobot.memory.stepwise_evidence import build_task_evidence, persist_task_evidence
from nanobot.memory.task_outcome import FAILED_STEPS, NORMAL_STOPS, resolve_task_outcome


def capture_turn_evidence(connection: Any, workspace: str, ctx: Any, evolution: Any) -> None:
    events = tuple(getattr(ctx, "tool_evidence", ()) or getattr(ctx, "tool_events", ()))
    failed = [event for event in events if isinstance(event, dict) and event.get("status") in FAILED_STEPS]
    tools = tuple(str(item) for item in (ctx.tools_used or ()) if str(item))
    source_type = ctx.audit_run.source_type
    episode = correction = None
    if ctx.stop_reason in {"error", "tool_error"} or failed:
        failure = failed[-1] if failed else {}
        episode = record_failed_episode(
            connection, workspace=workspace, trace_id=ctx.audit_turn.trace_id,
            session_key=ctx.session_key, source_type=source_type,
            stop_reason=ctx.stop_reason if ctx.stop_reason in {"error", "tool_error"} else "tool_error",
            user_text=ctx.original_user_text,
            tools=tools or tuple(str(event.get("tool_name") or event.get("name") or "") for event in failed),
            failure_error_code=failure.get("error_code"), failure_error_type=failure.get("error_type"),
            failure_error_source=failure.get("error_source"), failure_retryability=failure.get("retryability"),
            failure_detail=failure.get("detail"),
        )
    elif ctx.stop_reason in NORMAL_STOPS:
        correction = link_successful_correction(
            connection, workspace=workspace, trace_id=ctx.audit_turn.trace_id,
            session_key=ctx.session_key, source_type=source_type, user_text=ctx.original_user_text, tools=tools,
        )
    outcome = resolve_task_outcome(
        recovery_status=episode.status if episode else None, steps=events, stop_reason=ctx.stop_reason,
    )
    message = getattr(ctx, "msg", None)
    metadata = getattr(message, "metadata", {}) or {}
    group = metadata.get("group_openid")
    if not group and metadata.get("chat_type") == "group":
        group = getattr(message, "chat_id", None)
    source_group = None
    if group:
        source_group = str(message.channel) + ":" + hashlib.sha256(str(group).encode()).hexdigest()
    asset_enabled = bool(getattr(evolution, "asset_layer_enabled", False))
    stepwise = bool(getattr(evolution, "stepwise_evidence_enabled", False)) and getattr(evolution, "stepwise_evidence_mode", "shadow") != "disabled"
    task = None
    if asset_enabled or stepwise:
        legacy_eligible = ctx.stop_reason not in {"error", "tool_error"} and bool(tools) and all(
            name in {"read_file", "list_dir", "find_files", "grep", "skill_catalog_search", "skill_read"} for name in tools
        )
        task = build_task_evidence(
            workspace=workspace, session_key=ctx.session_key, trace_id=ctx.audit_turn.trace_id,
            turn_id=ctx.audit_turn.turn_id, user_text=ctx.original_user_text, events=events,
            actual_outcome=outcome or "unknown",
            legacy_qualification="candidate_eligible" if legacy_eligible else "rejected_by_legacy_gate",
            mixed_risk_policy=str(getattr(evolution, "mixed_risk_policy", "manual")),
        )
        if episode is not None and episode.recovery_trace_id:
            task = replace(task, source_trace_ids=tuple(dict.fromkeys(
                (*task.source_trace_ids, episode.recovery_trace_id),
            )))
        persist_task_evidence(connection, task)
        if asset_enabled:
            project_task_asset(connection, task.task_id, source_group=source_group)
    if correction is not None:
        # Refresh only the already-recorded original failure, never historical backfill.
        original = connection.execute(
            "SELECT t.* FROM evolution_task_evidence t JOIN evolution_task_links l ON l.task_id=t.task_id "
            "WHERE t.workspace=? AND l.source_type='trace' AND l.source_id=?",
            (workspace, correction.failure_trace_id),
        ).fetchone()
        if original is not None:
            original_steps = [dict(row) for row in connection.execute(
                "SELECT * FROM evolution_step_evidence WHERE task_id=? ORDER BY sequence_no", (original["task_id"],),
            )]
            corrected_outcome = resolve_task_outcome(recovery_status=correction.status, steps=original_steps)
            refreshed = build_task_evidence(
                workspace=workspace, session_key=original["session_key"], trace_id=correction.failure_trace_id,
                turn_id=None, user_text=original["goal"] or "", events=original_steps,
                actual_outcome=corrected_outcome or "unknown", task_id=original["task_id"],
                legacy_qualification=original["legacy_qualification"],
                mixed_risk_policy=str(getattr(evolution, "mixed_risk_policy", "manual")),
            )
            traces = tuple(dict.fromkeys((*json.loads(original["source_trace_ids_json"]), ctx.audit_turn.trace_id)))
            persist_task_evidence(connection, replace(refreshed, source_trace_ids=traces))
            if asset_enabled and connection.execute(
                "SELECT 1 FROM memory_assets WHERE workspace=? AND origin_table='evolution_task_evidence' AND origin_id=?",
                (workspace, original["task_id"]),
            ).fetchone():
                project_task_asset(connection, original["task_id"])
    # The legacy candidate stream remains gated independently until its removal phase.
    if outcome != "success" or (
        stepwise and getattr(evolution, "stepwise_evidence_mode", "shadow") == "enforced"
        and task is not None and task.qualification != "candidate_eligible"
    ):
        return
    persist_turn_semantic_evidence(
        connection, workspace=workspace, trace_id=ctx.audit_turn.trace_id, turn_id=ctx.audit_turn.turn_id,
        session_key=ctx.session_key, source_type=source_type, outcome="success", user_text=ctx.original_user_text,
        tools=tools,
    )
