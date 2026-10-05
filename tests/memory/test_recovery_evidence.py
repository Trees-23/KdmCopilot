from __future__ import annotations

from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.recovery_evidence import (
    link_successful_correction,
    list_recovery_reviews,
    record_failed_episode,
    review_recovery_episodes,
)


def test_recovery_case_requires_repeated_verified_corrections_in_one_session(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    for index in range(3):
        failed = record_failed_episode(
            connection,
            workspace=workspace,
            trace_id=f"failure-{index}",
            session_key="qq:group-test",
            source_type="user",
            stop_reason="tool_error",
            user_text="请查询当前项目有哪些可用 Skill，token=private-value",
            tools=("skill_read",),
        )
        assert failed is not None
        linked = link_successful_correction(
            connection,
            workspace=workspace,
            trace_id=f"success-{index}",
            session_key="qq:group-test",
            source_type="user",
            user_text="请查询当前项目有哪些可用 Skill，并按名称返回结构化清单",
            tools=("skill_read",),
        )
        assert linked is not None
        assert linked.status == "recovered"

    reviews = review_recovery_episodes(connection, workspace=workspace)
    assert len(reviews) == 1
    assert reviews[0].status == "passed"
    cards = list_recovery_reviews(connection, workspace=workspace)
    assert len(cards) == 1
    assert cards[0]["episode_count"] == 3
    assert "private-value" not in str(cards[0])
    assert "[REDACTED:CREDENTIAL]" in str(cards[0])
    connection.close()


def test_recovery_does_not_link_an_unrelated_or_unverified_turn(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    insufficient = record_failed_episode(
        connection,
        workspace=workspace,
        trace_id="failure-no-tool",
        session_key="session-a",
        source_type="user",
        stop_reason="error",
        user_text="请查询当前项目有哪些可用 Skill",
        tools=(),
    )
    assert insufficient is not None
    assert insufficient.status == "insufficient_recovery_evidence"

    unsafe = record_failed_episode(
        connection,
        workspace=workspace,
        trace_id="failure-write",
        session_key="session-a",
        source_type="user",
        stop_reason="tool_error",
        user_text="请修改项目中的说明文件",
        tools=("write_file",),
    )
    assert unsafe is not None
    assert unsafe.status == "insufficient_recovery_evidence"

    failed = record_failed_episode(
        connection,
        workspace=workspace,
        trace_id="failure-query",
        session_key="session-a",
        source_type="user",
        stop_reason="tool_error",
        user_text="请查询当前项目有哪些可用 Skill",
        tools=("skill_read",),
    )
    assert failed is not None
    unrelated = link_successful_correction(
        connection,
        workspace=workspace,
        trace_id="success-edit",
        session_key="session-a",
        source_type="user",
        user_text="请修改项目中的说明文件",
        tools=("read_file",),
    )
    assert unrelated is None
    status = connection.execute(
        "SELECT status FROM recovery_episodes WHERE failure_trace_id='failure-query'"
    ).fetchone()[0]
    assert status == "failed"
    connection.close()


def test_m17_two_different_file_names_share_a_structured_family(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    for index in (16, 17):
        failed = record_failed_episode(
            connection,
            workspace=workspace,
            trace_id=f"m17-failure-{index}",
            session_key="qq:single-long-session",
            source_type="user",
            stop_reason="tool_error",
            user_text=f"请严格读取不存在的 m{index}-qq-recovery-check.txt",
            tools=("read_file",),
            failure_error_code="file_not_found",
            failure_error_type="FileNotFoundError",
        )
        assert failed is not None
        assert link_successful_correction(
            connection,
            workspace=workspace,
            trace_id=f"m17-success-{index}",
            session_key="qq:single-long-session",
            source_type="user",
            user_text=f"刚才路径写错了，请读取 SOUL-{index}.md",
            tools=("read_file",),
        ) is not None

    rows = connection.execute(
        "SELECT operation_family,failure_family,task_family,correction_family FROM recovery_episodes"
    ).fetchall()
    assert {tuple(row) for row in rows} == {
        ("file_read", "resource_not_found", "workspace_file_lookup", "correct_path")
    }
    reviews = review_recovery_episodes(connection, workspace=workspace)
    assert len(reviews) == 1
    assert reviews[0].status == "passed"
    assert len(reviews[0].episode_ids) == 2
    connection.close()


def test_m17_single_recovery_or_different_error_family_does_not_create_issue(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    record_failed_episode(
        connection, workspace=workspace, trace_id="single-failure", session_key="qq",
        source_type="user", stop_reason="tool_error", user_text="请读取不存在的 missing.md",
        tools=("read_file",), failure_error_code="file_not_found",
    )
    assert link_successful_correction(
        connection, workspace=workspace, trace_id="single-success", session_key="qq",
        source_type="user", user_text="路径写错了，请读取 README.md", tools=("read_file",),
    ) is not None
    record_failed_episode(
        connection, workspace=workspace, trace_id="permission-failure", session_key="qq",
        source_type="user", stop_reason="tool_error", user_text="请读取受限的 private.md",
        tools=("read_file",), failure_error_code="permission_denied",
    )
    assert link_successful_correction(
        connection, workspace=workspace, trace_id="permission-success", session_key="qq",
        source_type="user", user_text="路径写错了，请读取 README.md", tools=("read_file",),
    ) is not None
    reviews = review_recovery_episodes(connection, workspace=workspace)
    assert len(reviews) == 2
    assert all(review.status == "insufficient_recovery_evidence" for review in reviews)
    from nanobot.memory.failure_issues import create_failure_issues

    assert create_failure_issues(connection, workspace=workspace) == ()
    connection.close()


def test_m17_rolling_window_excludes_stale_recovery_evidence(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    for index in range(2):
        record_failed_episode(
            connection, workspace=workspace, trace_id=f"stale-failure-{index}", session_key="qq",
            source_type="user", stop_reason="tool_error", user_text="请读取不存在的 stale.md",
            tools=("read_file",), failure_error_code="file_not_found",
        )
        assert link_successful_correction(
            connection, workspace=workspace, trace_id=f"stale-success-{index}", session_key="qq",
            source_type="user", user_text="路径写错了，请读取 README.md", tools=("read_file",),
        ) is not None
    connection.execute(
        "UPDATE recovery_episodes SET recovered_at=?,created_at=?",
        ("2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
    )
    connection.commit()
    reviews = review_recovery_episodes(connection, workspace=workspace)
    assert reviews == ()
    connection.close()
