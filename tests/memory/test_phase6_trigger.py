from __future__ import annotations

from datetime import UTC, datetime

from nanobot.config.schema import Phase6Config
from nanobot.cron.types import CronSchedule
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.phase6_trigger import (
    PHASE6_REVIEW_JOB_ID,
    build_phase6_review_job,
    register_phase6_review_job,
    run_phase6_review_scan,
)


class _Cron:
    def __init__(self) -> None:
        self.jobs = []

    def register_system_job(self, job) -> None:
        self.jobs.append(job)


def test_builds_protected_daily_beijing_cron_and_skips_disabled_config() -> None:
    job = build_phase6_review_job()
    assert job.id == PHASE6_REVIEW_JOB_ID
    assert job.payload.kind == "system_event"
    assert job.schedule == CronSchedule(kind="cron", expr="0 14 * * *", tz="Asia/Shanghai")

    cron = _Cron()
    assert register_phase6_review_job(cron, Phase6Config(enabled=False)) is False
    assert cron.jobs == []
    assert register_phase6_review_job(cron, Phase6Config(enabled=True)) is True
    assert len(cron.jobs) == 1


def test_production_scan_uses_trace_projection_and_records_cycle(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = open_maintenance_db(workspace)
    now = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO trace_index(trace_id,workspace,started_at,ended_at,outcome,summary,"
        "event_count,tool_count,redaction_version,source_path,created_at,updated_at) "
        "VALUES('t1',?,?,?,?,?,?,?,?,?,?,?)",
        (str(workspace), now, now, "success", "read project status", 1, 1, "v1", "derived://trace", now, now),
    )
    connection.commit()
    connection.close()

    result = run_phase6_review_scan(str(workspace), Phase6Config(enabled=True, min_repeat_count=3))
    assert result.status == "completed"
    evidence = (workspace / ".nanobot" / "phase6" / "evidence.jsonl").read_text(encoding="utf-8")
    assert '"event": "cycle"' in evidence
