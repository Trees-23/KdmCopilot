from __future__ import annotations

from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.recovery_evidence import (
    link_successful_correction,
    record_failed_episode,
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

    rows = connection.execute("SELECT failure_goal,status FROM recovery_episodes").fetchall()
    assert len(rows) == 3 and all(row[1] == "recovered" for row in rows)
    assert "private-value" not in str([tuple(row) for row in rows])
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
    assert unsafe.status == "failed"

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
    connection.close()
