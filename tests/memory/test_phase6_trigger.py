from __future__ import annotations

from nanobot.config.schema import Config, Phase6Config, Phase6EvolutionConfig
from nanobot.cron.types import CronSchedule
from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.phase6_trigger import (
    PHASE6_REVIEW_JOB_ID,
    build_phase6_review_job,
    register_phase6_review_job,
    run_phase6_review_scan,
)
from nanobot.memory.semantic_evidence import persist_turn_semantic_evidence


class _Cron:
    def __init__(self) -> None:
        self.jobs = []

    def register_system_job(self, job) -> None:
        self.jobs.append(job)


def test_runtime_config_normalizes_application_root_config() -> None:
    config = Config(phase6=Phase6Config(enabled=True))
    runtime = Phase6RuntimeConfig.from_config(config)
    assert runtime.enabled is True


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


def _insert_semantic_evidence(workspace, count: int) -> None:
    connection = open_maintenance_db(workspace)
    for index in range(count):
        persist_turn_semantic_evidence(
            connection,
            workspace=str(workspace.resolve()),
            trace_id=f"semantic-repeat-{index}",
            turn_id=f"turn-{index}",
            session_key=f"session-{index}",
            source_type="user",
            outcome="success",
            user_text="请查看当前工作区有哪些可用 Skill，并按名称列出",
            tools=("skill_read",),
        )
    connection.close()


def test_production_scan_generates_candidate_proposal_at_three_semantic_repeats(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _insert_semantic_evidence(workspace, 3)

    result = run_phase6_review_scan(
        str(workspace),
        Phase6Config(enabled=True, evolution=Phase6EvolutionConfig(ab_evaluation_mode="disabled")),
    )

    assert result.status == "completed"
    assert len(result.proposal_ids) == 1
    connection = open_maintenance_db(workspace)
    proposal = connection.execute(
        "SELECT source_kind,target,status FROM skill_proposals"
    ).fetchone()
    assert tuple(proposal) == ("shared", "git_pr_proposal", "eligible_for_confirmation")
    assert connection.execute("SELECT gate_result FROM eval_runs").fetchone()[0] == "eligible_for_confirmation"
    connection.close()

    # The next daily run sees the same evidence and remains idempotent.
    repeated = run_phase6_review_scan(
        str(workspace),
        Phase6Config(enabled=True, evolution=Phase6EvolutionConfig(ab_evaluation_mode="disabled")),
    )
    assert repeated.status == "completed"
    assert repeated.proposal_ids == ()
    assert repeated.deduplicated_task_keys


def test_production_scan_does_not_generate_before_three_repeats(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _insert_semantic_evidence(workspace, 2)

    result = run_phase6_review_scan(str(workspace), Phase6Config(enabled=True))

    assert result.status == "completed"
    assert result.selected_tasks == ()
    connection = open_maintenance_db(workspace)
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0
    connection.close()
