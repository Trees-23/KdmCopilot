"""Protected Gateway Cron trigger for the Phase 6 review scan.

This adapter owns only scheduling and the bounded Trace/Case scan.  Candidate
Skill generation remains a separate, evaluated stage; a scheduled scan must
never turn a natural-language Agent Cron job into a write or publish path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.memory.continuous import (
    EvidenceRecord,
    Phase6RuntimeConfig,
    append_evidence,
    load_failed_trace_records,
    run_cycle,
)
from nanobot.memory.db import connect_memory_db
from nanobot.memory.evolution_orchestrator import run_fixture_review_cycle
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.trace_indexer import index_audit_root

PHASE6_REVIEW_JOB_ID = "phase6-evolution-review"


@dataclass(frozen=True, slots=True)
class Phase6TriggerPolicy:
    """Stable production schedule policy."""

    cron_expression: str = "0 14 * * *"
    timezone: str = "Asia/Shanghai"


def build_phase6_review_job(policy: Phase6TriggerPolicy | None = None) -> CronJob:
    """Build a protected system job; missed runs naturally move to next day."""

    selected = policy or Phase6TriggerPolicy()
    return CronJob(
        id=PHASE6_REVIEW_JOB_ID,
        name=PHASE6_REVIEW_JOB_ID,
        schedule=CronSchedule(
            kind="cron",
            expr=selected.cron_expression,
            tz=selected.timezone,
        ),
        payload=CronPayload(kind="system_event"),
    )


def register_phase6_review_job(cron: Any, config: Any, *, policy: Phase6TriggerPolicy | None = None) -> bool:
    """Register the protected job only while Phase 6 is enabled."""

    if not bool(getattr(config, "enabled", False)):
        return False
    if bool(getattr(config, "kill_switch", False)):
        return False
    cron.register_system_job(build_phase6_review_job(policy))
    return True


def run_phase6_review_scan(
    workspace: str | Path,
    config: Any,
    *,
    audit_root: str | Path | None = None,
    overlay_pipeline: Any | None = None,
    overlay_pipeline_factory: Any | None = None,
    notify_proposal: Any | None = None,
    notify_error: Any | None = None,
) -> Any:
    """Run the production Trace → Candidate → Gate → Proposal scan.

    Overlay and QQ delivery are optional adapters so the protected system Cron
    can remain safe in installations that have not configured a private
    checkout or a notification worker.  When supplied, the existing pipeline
    performs Draft PR/CI and delivery idempotency checks.
    """

    root = Path(workspace).expanduser().resolve()
    connection = connect_memory_db(root)
    try:
        apply_migrations(connection)
        runtime = Phase6RuntimeConfig.from_config(config)
        # The audit log is the source of truth for Trace data.  Refresh the
        # derived index before selecting candidates; otherwise the daily job
        # can run successfully while seeing an empty trace_index forever.
        if audit_root is not None:
            index_audit_root(Path(audit_root), root, connection=connection)
        if overlay_pipeline_factory is not None:
            try:
                overlay_pipeline = overlay_pipeline_factory(connection, root, config)
            except (OSError, RuntimeError, ValueError) as exc:
                # Keep the Trace→Proposal path available, but fail closed at
                # the remote handoff boundary when checkout/auth is invalid.
                overlay_pipeline = None
                append_evidence(
                    root,
                    EvidenceRecord(
                        "overlay_adapter",
                        datetime.now(UTC).isoformat(timespec="seconds"),
                        status="skipped",
                        reason=str(exc)[:300],
                    ),
                    runtime,
                )
                if notify_error is not None:
                    try:
                        notify_error(str(exc)[:300])
                    except Exception:
                        pass
        # Candidate selection intentionally consumes only the separately
        # persisted business-semantic projection.  ``trace_index.summary`` is
        # lifecycle metadata and must never become a Skill name or prompt.
        from nanobot.memory.semantic_evidence import (
            load_semantic_candidate_records,
            record_quality_review,
        )

        semantic_records = load_semantic_candidate_records(connection, str(root))
        cycle = run_cycle(
            connection,
            root,
            runtime,
            trace_records=semantic_records,
            failed_traces=load_failed_trace_records(connection),
        )
        if cycle.status != "completed":
            return cycle
        result = cycle
        if cycle.selected_tasks:
            from nanobot.memory.phase6_candidates import (
                build_candidate_specs,
                evaluate_candidate_quality,
            )

            accepted = []
            for candidate in cycle.selected_tasks:
                quality = evaluate_candidate_quality(candidate)
                record_quality_review(
                    connection,
                    workspace=str(root),
                    semantic_key=candidate.task_key,
                    evidence_ids=candidate.evidence_ids,
                    status=quality.status,
                    reason_code=quality.reason_code or "passed",
                    reason_text=quality.reason_text or "业务语义、范围、结果和已验证操作均完整。",
                )
                if quality.status == "passed":
                    accepted.append(candidate)
                else:
                    append_evidence(
                        root,
                        EvidenceRecord(
                            "semantic_quality_gate",
                            datetime.now(UTC).isoformat(timespec="seconds"),
                            trace_ids=candidate.trace_ids,
                            status="rejected_by_quality_gate",
                            reason=quality.reason_text,
                        ),
                        runtime,
                    )

            built = build_candidate_specs(
                connection,
                accepted,
                min_repeat_count=runtime.min_repeat_count,
            )
            result = run_fixture_review_cycle(
                connection,
                root,
                runtime,
                trace_records=semantic_records,
                candidate_specs=built.specs,
                failed_traces=(),
                overlay_pipeline=overlay_pipeline,
            )
        if overlay_pipeline is not None:
            overlay_pipeline.retry_failed_handoffs(connection, config)
            # Draft PR creation and CI are separate phases. Poll existing
            # Proposal rows once per scheduled run; the pipeline records an
            # idempotent notification only after all checks pass.
            for row in connection.execute(
                "SELECT proposal_id FROM skill_proposals WHERE workspace=? AND target='git_pr_proposal' "
                "AND status='pr_created' ORDER BY updated_at",
                (str(root),),
            ).fetchall():
                overlay_pipeline.refresh_ci_and_notify(connection, str(row[0]))
        if notify_proposal is not None and hasattr(result, "proposal_ids"):
            for proposal_id in result.proposal_ids:
                notify_proposal(proposal_id)
        return result
    except Exception as exc:
        if notify_error is not None:
            try:
                notify_error(str(exc)[:300])
            except Exception:
                # A notification failure must not replace the original Cron
                # failure in the scheduler's durable error record.
                pass
        raise
    finally:
        connection.close()


__all__ = [
    "PHASE6_REVIEW_JOB_ID",
    "Phase6TriggerPolicy",
    "build_phase6_review_job",
    "register_phase6_review_job",
    "run_phase6_review_scan",
]
