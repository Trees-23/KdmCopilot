from __future__ import annotations

from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.recovery_evidence import (
    link_successful_correction,
    list_recovery_reviews,
    record_failed_episode,
    review_recovery_episodes,
)


def test_recovery_case_requires_repeated_cross_session_verified_corrections(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    workspace = str(tmp_path.resolve())
    for index in range(3):
        failed = record_failed_episode(
            connection,
            workspace=workspace,
            trace_id=f"failure-{index}",
            session_key=f"session-{index}",
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
            session_key=f"session-{index}",
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
