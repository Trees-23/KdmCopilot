from __future__ import annotations

from types import SimpleNamespace

from nanobot.agent.loop import AgentLoop, TurnKind
from nanobot.memory.continuous import TraceCandidate
from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.phase6_candidates import evaluate_candidate_quality
from nanobot.memory.semantic_evidence import (
    load_semantic_candidate_records,
    persist_turn_semantic_evidence,
    project_user_task,
)


def test_semantic_projection_is_redacted_and_requires_a_verified_operation() -> None:
    projection, reason = project_user_task(
        "请查看项目里的 Skill，token=super-secret-value 不要展示，并列出名称", ("skill_read",)
    )
    assert reason is None
    assert projection is not None
    assert "super-secret-value" not in projection.task_goal
    assert "[REDACTED:CREDENTIAL]" in projection.task_goal

    missing, reason = project_user_task("请查看项目里的 Skill", ())
    assert missing is None
    assert reason == "no_verified_operation"


def test_framework_json_is_not_semantic_evidence(tmp_path) -> None:
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    record = persist_turn_semantic_evidence(
        connection,
        workspace=str(tmp_path.resolve()),
        trace_id="trace-framework",
        turn_id="turn-framework",
        session_key="session",
        source_type="user",
        outcome="success",
        user_text='{"event_count": 17, "event_types": ["checkpoint_written"]}',
        tools=("skill_read",),
    )
    assert record is None
    assert load_semantic_candidate_records(connection, str(tmp_path.resolve())) == ()
    row = connection.execute(
        "SELECT evidence_status,rejection_reason FROM semantic_task_evidence WHERE trace_id='trace-framework'"
    ).fetchone()
    assert tuple(row) == ("insufficient_semantic_evidence", "framework_or_nonsemantic_input")
    connection.close()


def test_quality_gate_rejects_framework_candidate_and_accepts_complete_business_candidate() -> None:
    rejected = evaluate_candidate_quality(
        TraceCandidate("bad", ("a", "b", "c"), '{"event_count":17}', 3, 0.0)
    )
    assert rejected.status == "rejected_by_quality_gate"
    assert rejected.reason_code == "framework_event_evidence"

    accepted = evaluate_candidate_quality(
        TraceCandidate(
            "semantic:demo", ("a", "b", "c"), "列出当前项目的可用 Skill", 3, 0.0,
            "查询信息", "当前项目工作区", "返回结构化查询结果", ("skill_read",), ("e1", "e2", "e3"),
        )
    )
    assert accepted.status == "passed"


def test_agent_loop_captures_only_successful_user_semantic_evidence(tmp_path) -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = str(tmp_path)
    loop.phase6_config = SimpleNamespace(enabled=True)
    context = SimpleNamespace(
        kind=TurnKind.USER,
        original_user_text="请查看当前工作区有哪些可用 Skill，并按名称列出",
        audit_turn=SimpleNamespace(trace_id="trace-loop", turn_id="turn-loop"),
        audit_run=SimpleNamespace(source_type="user"),
        stop_reason="completed",
        request_context=None,
        session_key="session-loop",
        tools_used=["skill_read"],
    )

    loop._capture_semantic_evolution_evidence(context)

    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    row = connection.execute(
        "SELECT task_goal,evidence_status FROM semantic_task_evidence WHERE trace_id='trace-loop'"
    ).fetchone()
    assert tuple(row) == ("请查看当前工作区有哪些可用 Skill，并按名称列出", "eligible")
    connection.close()
