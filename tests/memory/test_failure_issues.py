from __future__ import annotations

import pytest

from nanobot.config.schema import Phase6Config, Phase6EvolutionConfig
from nanobot.memory.db import connect_memory_db
from nanobot.memory.failure_issues import (
    apply_failure_issue_action,
    create_failure_issues,
    list_failure_issues,
)
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.phase6_trigger import (
    RECOVERY_ISSUE_REVIEW_JOB_ID,
    build_recovery_issue_review_job,
    register_recovery_issue_review_job,
    run_recovery_issue_scan,
)
from nanobot.memory.recovery_evidence import (
    link_successful_correction,
    record_failed_episode,
    review_recovery_episodes,
)


def _seed_recovery_review(connection, workspace: str) -> None:
    for index in range(3):
        record_failed_episode(
            connection,
            workspace=workspace,
            trace_id=f"failure-issue-{index}",
            session_key=f"issue-session-{index}",
            source_type="user",
            stop_reason="tool_error",
            user_text="请查询当前工作区有哪些可用 Skill",
            tools=("skill_read",),
        )
        assert link_successful_correction(
            connection,
            workspace=workspace,
            trace_id=f"success-issue-{index}",
            session_key=f"issue-session-{index}",
            source_type="user",
            user_text="请查询当前工作区有哪些可用 Skill 并返回清单",
            tools=("skill_read",),
        )
    assert review_recovery_episodes(connection, workspace=workspace)[0].status == "passed"


def test_failure_issue_materialization_and_action_idempotency(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)

    created = create_failure_issues(connection, workspace=workspace)
    assert len(created) == 1
    assert create_failure_issues(connection, workspace=workspace) == ()
    issue = list_failure_issues(connection, workspace=workspace)[0]
    assert issue["status"] == "pending_review"
    result = apply_failure_issue_action(
        connection,
        workspace=workspace,
        issue_id=issue["issue_id"],
        action="record_case",
        actor_openid="admin-1",
        group_openid="group-1",
        idempotency_key="issue-action-1",
    )
    assert result["status"] == "recorded_case"
    replay = apply_failure_issue_action(
        connection,
        workspace=workspace,
        issue_id=issue["issue_id"],
        action="record_case",
        actor_openid="admin-1",
        group_openid="group-1",
        idempotency_key="issue-action-1",
    )
    assert replay["idempotent"] is True
    connection.close()


def test_failure_issue_rejects_invalid_status_and_candidate_toggle(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]
    apply_failure_issue_action(
        connection,
        workspace=workspace,
        issue_id=issue_id,
        action="reject",
        actor_openid="admin-1",
        group_openid="group-1",
        note="一次性故障，不沉淀",
    )
    with pytest.raises(ValueError, match="does not accept"):
        apply_failure_issue_action(
            connection,
            workspace=workspace,
            issue_id=issue_id,
            action="record_memory",
            actor_openid="admin-1",
            group_openid="group-1",
        )
    connection.close()


def test_recovery_issue_cron_is_noon_beijing_and_scan_is_idempotent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = Phase6Config(
        enabled=True,
        evolution=Phase6EvolutionConfig(
            recovery_review_enabled=True,
            recovery_notifications_enabled=False,
        ),
    )
    job = build_recovery_issue_review_job()
    assert job.id == RECOVERY_ISSUE_REVIEW_JOB_ID
    assert job.schedule.expr == "0 12 * * *"
    assert job.schedule.tz == "Asia/Shanghai"

    class Cron:
        def __init__(self):
            self.jobs = []

        def register_system_job(self, value):
            self.jobs.append(value)

    cron = Cron()
    assert register_recovery_issue_review_job(cron, config)
    assert len(cron.jobs) == 1
    result = run_recovery_issue_scan(workspace, config)
    assert result["status"] == "completed"
    assert result["created"] == ()
    connection = connect_memory_db(workspace)
    _seed_recovery_review(connection, str(workspace.resolve()))
    connection.close()
    result = run_recovery_issue_scan(workspace, config)
    assert result["status"] == "completed"
    assert len(result["created"]) == 1
    assert run_recovery_issue_scan(workspace, config)["created"] == ()


def test_recovery_issue_scan_stays_disabled_with_kill_switch(tmp_path) -> None:
    config = Phase6Config(enabled=True, kill_switch=True)
    assert run_recovery_issue_scan(tmp_path, config)["status"] == "disabled"
