"""Protected Gateway Cron trigger for the Phase 6 review scan.

This adapter owns only scheduling and the bounded Trace/Case scan.  Candidate
Skill generation remains a separate, evaluated stage; a scheduled scan must
never turn a natural-language Agent Cron job into a write or publish path.
"""

from __future__ import annotations

import asyncio
import json
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
from nanobot.memory.derivation import digest
from nanobot.memory.evolution_orchestrator import run_fixture_review_cycle
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.trace_indexer import index_audit_root

PHASE6_REVIEW_JOB_ID = "phase6-evolution-review"
RECOVERY_ISSUE_REVIEW_JOB_ID = "phase6-recovery-issue-review"


@dataclass(frozen=True, slots=True)
class Phase6TriggerPolicy:
    """Stable production schedule policy."""

    cron_expression: str = "0 14 * * *"
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True, slots=True)
class RecoveryIssueTriggerPolicy:
    """Daily human-review digest schedule for recovered failures."""

    cron_expression: str = "0 12 * * *"
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


def build_recovery_issue_review_job(policy: RecoveryIssueTriggerPolicy | None = None) -> CronJob:
    selected = policy or RecoveryIssueTriggerPolicy()
    return CronJob(
        id=RECOVERY_ISSUE_REVIEW_JOB_ID,
        name=RECOVERY_ISSUE_REVIEW_JOB_ID,
        schedule=CronSchedule(kind="cron", expr=selected.cron_expression, tz=selected.timezone),
        payload=CronPayload(kind="system_event"),
    )


def register_recovery_issue_review_job(cron: Any, config: Any, *, policy: RecoveryIssueTriggerPolicy | None = None) -> bool:
    if not bool(getattr(config, "enabled", False)) or bool(getattr(config, "kill_switch", False)):
        return False
    evolution = getattr(config, "evolution", config)
    if not bool(getattr(evolution, "recovery_review_enabled", True)):
        return False
    cron.register_system_job(build_recovery_issue_review_job(policy))
    return True


def run_recovery_issue_scan(
    workspace: str | Path,
    config: Any,
    *,
    notify_issue: Any | None = None,
) -> dict[str, Any]:
    """Create idempotent human-review Issues from M13 recovery reviews."""

    root = Path(workspace).expanduser().resolve()
    connection = connect_memory_db(root)
    try:
        apply_migrations(connection)
        evolution = getattr(config, "evolution", config)
        if not bool(getattr(config, "enabled", False)) or bool(getattr(config, "kill_switch", False)):
            return {"status": "disabled", "created": (), "notified": 0}
        from nanobot.memory.failure_issues import create_failure_issues, list_failure_issues
        from nanobot.memory.recovery_evidence import review_recovery_episodes

        review_recovery_episodes(connection, workspace=str(root))
        created = create_failure_issues(connection, workspace=str(root))
        notified = 0
        if bool(getattr(evolution, "recovery_notifications_enabled", False)) and notify_issue is not None:
            groups = tuple(str(item) for item in getattr(evolution, "notification_groups", ()) or () if str(item))
            for issue_id in created:
                for group in groups:
                    try:
                        notified += int(notify_issue(issue_id, group) or 0)
                    except Exception:
                        continue
        pending = list_failure_issues(connection, workspace=str(root), statuses=("pending_review",))
        append_evidence(
            root,
            EvidenceRecord(
                "recovery_issue_cycle",
                datetime.now(UTC).isoformat(timespec="seconds"),
                status="ok",
                metrics={"created_issues": len(created), "pending_issues": len(pending), "notified": notified},
            ),
            Phase6RuntimeConfig.from_config(config),
        )
        return {"status": "completed", "created": created, "notified": notified, "pending": len(pending)}
    finally:
        connection.close()


def _load_repair_candidates(connection: Any, workspace: str) -> tuple[tuple[Any, ...], tuple[dict[str, Any], ...]]:
    """Load queued repair drafts as bounded M15 CandidateSpecs and evidence."""

    from nanobot.memory.evolution_orchestrator import CandidateSpec

    rows = connection.execute(
        """SELECT c.candidate_id,c.issue_id,c.skill_id,c.skill_name,c.baseline_revision_id,
                  c.candidate_revision_id,c.candidate_hash,c.candidate_content,
                  i.task_goal,i.failure_class,i.correction_goal,i.episode_ids_json
           FROM failure_issue_candidates c JOIN failure_issues i ON i.issue_id=c.issue_id
           WHERE c.workspace=? AND c.status='queued' ORDER BY c.created_at LIMIT 20""",
        (workspace,),
    ).fetchall()
    specs: list[Any] = []
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            episode_ids = tuple(str(item) for item in json.loads(row[11] or "[]") if str(item))
        except (TypeError, ValueError, json.JSONDecodeError):
            episode_ids = ()
        episode_rows = connection.execute(
            "SELECT failure_trace_id,recovery_trace_id FROM recovery_episodes WHERE episode_id IN ({})".format(
                ",".join("?" for _ in episode_ids) or "NULL"
            ), episode_ids,
        ).fetchall() if episode_ids else []
        trace_ids = tuple(str(value) for pair in episode_rows for value in pair if value)
        cases = tuple({
            "case_id": f"recovery-case:{episode_id}",
            "prompt": str(row[8] or "").strip(),
            "expected": str(row[10] or "恢复成功并遵守范围边界").strip(),
        } for episode_id in episode_ids)
        if not trace_ids or not cases:
            continue
        task_key = f"recovery:{row[0]}"
        specs.append(CandidateSpec(
            task_key=task_key, skill_id=str(row[2]), skill_name=str(row[3]), source_kind="shared",
            baseline_revision_id=str(row[4]), candidate_revision_id=str(row[5]),
            baseline_hash=digest(""), candidate_hash=str(row[6]), cases=cases,
            fixture_hash="sha256:" + str(row[0]).replace("failure-candidate:", ""),
            case_ids=tuple(item["case_id"] for item in cases), candidate_content=str(row[7]),
            holdout_case_ids=tuple(item["case_id"] for item in cases[-max(2, len(cases) // 5):]),
        ))
        records.extend({
            "trace_id": trace_id, "summary": str(row[8] or "恢复失败任务"), "intent": "排查问题",
            "input_scope": str(row[8] or "恢复失败任务"),
            "expected_outcome": str(row[10] or "恢复成功并遵守范围边界"),
            "tools": ("skill_read",), "outcome": "success", "task_key": task_key,
            "evidence_id": f"recovery-evidence:{episode_id}",
        } for episode_id, trace_id in zip(episode_ids, trace_ids))
    return tuple(specs), tuple(records)


def _mark_repair_candidate(
    connection: Any,
    candidate_hash: str,
    *,
    status: str,
    reason: str,
) -> None:
    connection.execute(
        "UPDATE failure_issue_candidates SET status=?,reason=?,updated_at=? WHERE candidate_hash=? AND status='queued'",
        (status, reason[:500], datetime.now(UTC).isoformat(timespec="seconds"), candidate_hash),
    )
    connection.commit()


async def run_phase6_review_scan_async(
    workspace: str | Path,
    config: Any,
    *,
    audit_root: str | Path | None = None,
    overlay_pipeline: Any | None = None,
    overlay_pipeline_factory: Any | None = None,
    notify_proposal: Any | None = None,
    notify_error: Any | None = None,
    active_agent: Any | None = None,
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
        # Recovery lessons are a separate M13 path.  They create only
        # redacted Case-review metadata and can never create a Skill or PR.
        from nanobot.memory.recovery_evidence import review_recovery_episodes

        review_recovery_episodes(connection, workspace=str(root))
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
        repair_proposal_ids: tuple[str, ...] = ()
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
            from nanobot.memory.ab_evaluation import evaluate_candidate_specs

            ab_result = await evaluate_candidate_specs(
                connection,
                root,
                built.specs,
                runtime,
                active_agent=active_agent,
            )
            append_evidence(
                root,
                EvidenceRecord(
                    "ab_quality_evaluation",
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    status=ab_result.status,
                    metrics={
                        "evaluated_count": ab_result.evaluated_count,
                        "model_calls_used": ab_result.model_calls_used,
                        "approved_count": len(ab_result.approved_specs),
                    },
                    reason=ab_result.reason,
                ),
                runtime,
            )
            if ab_result.status == "budget_exhausted" and notify_error is not None:
                try:
                    notify_error("Skill A/B 质量评测今日预算已耗尽；本轮候选已停止，不会创建审批。")
                except Exception:
                    pass
            specs = built.specs if runtime.ab_evaluation_mode == "shadow" else ab_result.approved_specs
            result = run_fixture_review_cycle(
                connection,
                root,
                runtime,
                trace_records=semantic_records,
                candidate_specs=specs,
                failed_traces=(),
                overlay_pipeline=overlay_pipeline,
            )
        evolution = getattr(config, "evolution", config)
        if bool(getattr(evolution, "recovery_skill_candidate_enabled", False)):
            repair_specs, repair_records = _load_repair_candidates(connection, str(root))
            if repair_specs:
                from nanobot.memory.ab_evaluation import evaluate_candidate_specs

                repair_ab = await evaluate_candidate_specs(
                    connection, root, repair_specs, runtime, active_agent=active_agent,
                )
                approved_hashes = {spec.candidate_hash for spec in repair_ab.approved_specs}
                for spec in repair_specs:
                    if spec.candidate_hash not in approved_hashes:
                        row = connection.execute(
                            "SELECT reason_code FROM ab_evaluations WHERE workspace=? AND candidate_hash=? "
                            "ORDER BY created_at DESC LIMIT 1", (str(root), spec.candidate_hash),
                        ).fetchone()
                        if repair_ab.status == "budget_exhausted" and row is None:
                            continue
                        _mark_repair_candidate(
                            connection, spec.candidate_hash, status="failed",
                            reason=str(row[0]) if row else (repair_ab.reason or "A/B 质量门禁未通过"),
                        )
                if repair_ab.approved_specs:
                    repair_cycle = run_fixture_review_cycle(
                        connection, root, runtime, trace_records=repair_records,
                        candidate_specs=repair_ab.approved_specs, failed_traces=(),
                        overlay_pipeline=overlay_pipeline,
                    )
                    repair_proposal_ids = tuple(repair_cycle.proposal_ids)
                    for spec in repair_ab.approved_specs:
                        _mark_repair_candidate(connection, spec.candidate_hash, status=(
                            "passed" if spec.task_key in set(repair_cycle.proposal_ids) else "passed"
                        ), reason="M15 A/B 与 Proposal 门禁通过")
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
        if notify_proposal is not None:
            for proposal_id in repair_proposal_ids:
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


def run_phase6_review_scan(
    workspace: str | Path,
    config: Any,
    **kwargs: Any,
) -> Any:
    """Synchronous compatibility wrapper used by maintenance callers/tests."""

    return asyncio.run(run_phase6_review_scan_async(workspace, config, **kwargs))


__all__ = [
    "PHASE6_REVIEW_JOB_ID",
    "RECOVERY_ISSUE_REVIEW_JOB_ID",
    "Phase6TriggerPolicy",
    "RecoveryIssueTriggerPolicy",
    "build_phase6_review_job",
    "build_recovery_issue_review_job",
    "register_phase6_review_job",
    "register_recovery_issue_review_job",
    "run_recovery_issue_scan",
    "run_phase6_review_scan",
    "run_phase6_review_scan_async",
]
