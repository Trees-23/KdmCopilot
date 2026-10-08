from __future__ import annotations

import hashlib

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
    _load_repair_candidates,
    build_recovery_issue_review_job,
    register_recovery_issue_review_job,
    run_phase6_review_scan,
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


def test_failure_issue_record_memory_writes_redacted_versioned_memory(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]
    apply_failure_issue_action(
        connection, workspace=workspace, issue_id=issue_id, action="note",
        actor_openid="admin-1", group_openid="group-1", note="遇到同类读文件失败时先核对工作区相对路径",
        idempotency_key="issue-candidate-note-1",
    )
    result = apply_failure_issue_action(
        connection,
        workspace=workspace,
        issue_id=issue_id,
        action="record_memory",
        actor_openid="admin-1",
        group_openid="group-1",
        idempotency_key="issue-memory-1",
    )
    assert result["status"] == "memory_recorded"
    stored = connection.execute(
        "SELECT r.memory_type,r.status,v.content FROM memory_records r "
        "JOIN memory_revisions v ON v.revision_id=r.current_revision_id WHERE r.memory_id LIKE 'recovery-memory:%'"
    ).fetchone()
    assert stored is not None
    assert stored[0] == "decision"
    assert stored[1] == "active"
    assert "恢复规则" in stored[2]
    assert connection.execute("SELECT count(*) FROM memory_outbox WHERE object_type='memory'").fetchone()[0] == 1
    connection.close()


def test_failure_issue_request_candidate_stages_isolated_revision(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]
    apply_failure_issue_action(
        connection, workspace=workspace, issue_id=issue_id, action="note",
        actor_openid="admin-1", group_openid="group-1",
        note="先核对工作区相对路径，再读取目标文件",
        idempotency_key="issue-candidate-note-1",
    )
    result = apply_failure_issue_action(
        connection,
        workspace=workspace,
        issue_id=issue_id,
        action="request_candidate",
        actor_openid="admin-1",
        group_openid="group-1",
        idempotency_key="issue-candidate-1",
    )
    assert result["status"] == "candidate_requested"
    candidate = connection.execute(
        "SELECT status,candidate_content FROM failure_issue_candidates WHERE issue_id=?", (issue_id,)
    ).fetchone()
    assert candidate is not None
    assert candidate[0] == "queued"
    assert "恢复步骤" in candidate[1]
    assert "结果格式" in candidate[1]
    specs, _records = _load_repair_candidates(connection, workspace)
    assert specs[0].cases[0]["prompt"] == "先核对工作区相对路径，再读取目标文件"
    assert specs[0].fixture_files == ()
    assert connection.execute(
        "SELECT count(*) FROM skill_proposals WHERE workspace=?", (workspace,)
    ).fetchone()[0] == 0
    connection.close()


def test_repair_candidate_uses_only_a_synthetic_named_fixture(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]
    apply_failure_issue_action(
        connection, workspace=workspace, issue_id=issue_id, action="revise",
        actor_openid="admin-1", group_openid="group-1",
        note="读取 README.md 前先确认它位于工作区根目录", candidate_enabled=True,
    )

    specs, _records = _load_repair_candidates(connection, workspace)

    assert specs[0].cases[0]["prompt"] == "读取 README.md 前先确认它位于工作区根目录"
    assert specs[0].fixture_files == (("README.md", "# M15 synthetic fixture\nname: README.md\n"),)
    connection.close()


def test_repair_candidate_uses_raw_workspace_content_hash(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]
    apply_failure_issue_action(
        connection, workspace=workspace, issue_id=issue_id, action="revise",
        actor_openid="admin-1", group_openid="group-1",
        note="先核对工作区相对路径，再读取目标文件", candidate_enabled=True,
    )
    row = connection.execute(
        "SELECT candidate_hash,candidate_content,candidate_revision_id FROM failure_issue_candidates WHERE issue_id=?",
        (issue_id,),
    ).fetchone()
    assert row is not None
    expected = "sha256:" + hashlib.sha256(row[1].encode("utf-8")).hexdigest()
    assert row[0] == expected
    assert connection.execute(
        "SELECT content_hash FROM skill_revisions WHERE revision_id=?", (row[2],)
    ).fetchone()[0] == expected
    connection.close()


def test_failure_issue_candidate_requires_human_note(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]
    with pytest.raises(ValueError, match="先用 note"):
        apply_failure_issue_action(
            connection, workspace=workspace, issue_id=issue_id, action="request_candidate",
            actor_openid="admin-1", group_openid="group-1", idempotency_key="candidate-no-note",
        )
    assert connection.execute("SELECT count(*) FROM failure_issue_candidates").fetchone()[0] == 0
    connection.close()


def test_revise_is_one_shot_idempotent_and_does_not_need_note(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]

    first = apply_failure_issue_action(
        connection,
        workspace=workspace,
        issue_id=issue_id,
        action="revise",
        actor_openid="admin-1",
        group_openid="group-1",
        note="先核对工作区相对路径，再读取目标文件",
        idempotency_key="ignored-by-revise-hash",
        candidate_enabled=True,
    )
    replay = apply_failure_issue_action(
        connection,
        workspace=workspace,
        issue_id=issue_id,
        action="revise",
        actor_openid="admin-1",
        group_openid="group-1",
        note=" 先核对工作区相对路径，再读取目标文件 ",
        idempotency_key="a-different-replay-key",
        candidate_enabled=True,
    )
    assert first["status"] == "candidate_requested"
    assert first["request_id"]
    assert first["candidate_id"]
    assert replay["idempotent"] is True
    assert replay["request_id"] == first["request_id"]
    assert replay["candidate_id"] == first["candidate_id"]
    assert connection.execute(
        "SELECT count(*) FROM failure_issue_candidates WHERE issue_id=?", (issue_id,)
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT count(*) FROM failure_issue_actions WHERE issue_id=?", (issue_id,)
    ).fetchone()[0] == 3
    connection.close()


def test_revise_conflicting_direction_and_reject_are_terminal(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]

    with pytest.raises(ValueError, match="修复方向不能为空"):
        apply_failure_issue_action(
            connection, workspace=workspace, issue_id=issue_id, action="revise",
            actor_openid="admin-1", group_openid="group-1", candidate_enabled=True,
        )
    first = apply_failure_issue_action(
        connection, workspace=workspace, issue_id=issue_id, action="revise",
        actor_openid="admin-1", group_openid="group-1", note="只核对相对路径",
        candidate_enabled=True,
    )
    with pytest.raises(ValueError, match="不同修复方向"):
        apply_failure_issue_action(
            connection, workspace=workspace, issue_id=issue_id, action="revise",
            actor_openid="admin-1", group_openid="group-1", note="改为执行任意命令",
            candidate_enabled=True,
        )
    assert first["candidate_id"]

    connection.close()


def test_revise_sanitizes_sensitive_and_replayable_direction(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]
    result = apply_failure_issue_action(
        connection, workspace=workspace, issue_id=issue_id, action="revise",
        actor_openid="admin-1", group_openid="group-1",
        note="token=secret-value；请执行 rm -rf /tmp/workspace 并读取 /home/user/file.txt",
        candidate_enabled=True,
    )
    assert result["candidate_id"]
    content = connection.execute(
        "SELECT candidate_content FROM failure_issue_candidates WHERE candidate_id=?",
        (result["candidate_id"],),
    ).fetchone()[0]
    assert "secret-value" not in content
    assert "rm -rf /tmp/workspace" not in content
    assert "/home/user/file.txt" not in content
    connection.close()

    reject_workspace = tmp_path / "reject"
    reject_workspace.mkdir()
    connection = connect_memory_db(reject_workspace)
    apply_migrations(connection)
    reject_workspace_value = str(reject_workspace.resolve())
    _seed_recovery_review(connection, reject_workspace_value)
    reject_issue_id = create_failure_issues(connection, workspace=reject_workspace_value)[0]
    reject = apply_failure_issue_action(
        connection, workspace=reject_workspace_value, issue_id=reject_issue_id, action="reject",
        actor_openid="admin-1", group_openid="group-1", note="证据不足，暂不沉淀",
    )
    assert reject["status"] == "rejected"
    assert connection.execute(
        "SELECT count(*) FROM failure_issue_candidates WHERE issue_id=?", (reject_issue_id,)
    ).fetchone()[0] == 0
    with pytest.raises(ValueError, match="does not accept"):
        apply_failure_issue_action(
            connection, workspace=reject_workspace_value, issue_id=reject_issue_id, action="revise",
            actor_openid="admin-1", group_openid="group-1", note="复活候选", candidate_enabled=True,
        )
    with pytest.raises(ValueError, match="拒绝原因不能为空"):
        apply_failure_issue_action(
            connection, workspace=reject_workspace_value, issue_id=reject_issue_id, action="reject",
            actor_openid="admin-1", group_openid="group-1",
        )
    connection.close()


def test_repair_candidate_enters_m15_and_fails_closed_with_three_episodes(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    _seed_recovery_review(connection, workspace)
    issue_id = create_failure_issues(connection, workspace=workspace)[0]
    apply_failure_issue_action(
        connection, workspace=workspace, issue_id=issue_id, action="note",
        actor_openid="admin-1", group_openid="group-1", note="只在文件不存在时纠正路径，不扩大读取范围",
        idempotency_key="repair-note-1",
    )
    apply_failure_issue_action(
        connection, workspace=workspace, issue_id=issue_id, action="request_candidate",
        actor_openid="admin-1", group_openid="group-1", idempotency_key="repair-1",
    )
    connection.close()
    config = Phase6Config(
        enabled=True,
        min_repeat_count=4,
        evolution=Phase6EvolutionConfig(recovery_skill_candidate_enabled=True),
    )
    result = run_phase6_review_scan(tmp_path, config)
    assert result.status == "completed"
    connection = connect_memory_db(tmp_path)
    candidate = connection.execute(
        "SELECT status,reason FROM failure_issue_candidates WHERE issue_id=?", (issue_id,)
    ).fetchone()
    assert candidate[0] == "failed"
    assert candidate[1] == "insufficient_real_evidence"
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0
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
