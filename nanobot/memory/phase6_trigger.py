"""Protected Gateway Cron trigger for the Phase 6 review scan.

This adapter owns only scheduling and the bounded Trace/Case scan.  Candidate
Skill generation remains a separate, evaluated stage; a scheduled scan must
never turn a natural-language Agent Cron job into a write or publish path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.memory.continuous import (
    Phase6RuntimeConfig,
    load_failed_trace_records,
    load_trace_records,
    run_cycle,
)
from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations

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


def run_phase6_review_scan(workspace: str | Path, config: Any) -> Any:
    """Run one payload-free production scan under the existing lease/CAS rules."""

    root = Path(workspace).expanduser().resolve()
    connection = connect_memory_db(root)
    try:
        apply_migrations(connection)
        runtime = Phase6RuntimeConfig.from_config(config)
        return run_cycle(
            connection,
            root,
            runtime,
            trace_records=load_trace_records(connection),
            failed_traces=load_failed_trace_records(connection),
        )
    finally:
        connection.close()


__all__ = [
    "PHASE6_REVIEW_JOB_ID",
    "Phase6TriggerPolicy",
    "build_phase6_review_job",
    "register_phase6_review_job",
    "run_phase6_review_scan",
]
