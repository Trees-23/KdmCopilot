from __future__ import annotations

from types import SimpleNamespace

from nanobot.agent.loop import AgentLoop, TurnKind
from nanobot.memory.candidate_staging import (
    get_staged_candidate,
    list_staged_candidates,
    stage_task_candidate,
)
from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.phase6_trigger import run_phase6_review_scan
from nanobot.memory.stepwise_evidence import (
    build_step_evidence,
    build_task_evidence,
    classify_risk,
    load_stepwise_candidate_records,
    persist_task_evidence,
)


def _connection(tmp_path):
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    return connection


def test_risk_uses_capability_and_effect_not_a_whitelist() -> None:
    assert classify_risk("read_file") == "R0"
    assert classify_risk("web_fetch") == "R1"
    assert classify_risk("write_file", side_effect_class="filesystem_write") == "R2"
    assert classify_risk("exec", side_effect_class="process_execution") == "R3"
    assert classify_risk("read_file", event={"detail": "outside workspace boundary"}) == "R4"


def test_mixed_steps_keep_order_and_downgrade_only_the_task(tmp_path) -> None:
    events = (
        {"event_id": "e1", "tool_name": "read_file", "status": "ok", "safe_input_summary": "path=README.md", "verification_kind": "read_success"},
        {"event_id": "e2", "tool_name": "write_file", "status": "ok", "safe_input_summary": "write targets omitted", "verification_kind": "filesystem_after_state"},
        {"event_id": "e3", "tool_name": "read_file", "status": "ok", "safe_input_summary": "path=README.md", "verification_kind": "read_success"},
    )
    steps = build_step_evidence(events, trace_id="trace-mixed")
    assert [step.sequence_no for step in steps] == [0, 1, 2]
    assert [step.risk_level for step in steps] == ["R0", "R2", "R0"]
    assert [step.candidate_use for step in steps] == ["reusable", "abstract_only", "reusable"]

    task = build_task_evidence(
        workspace=str(tmp_path.resolve()), session_key="session", trace_id="trace-mixed", turn_id="turn-mixed",
        user_text="请读取并修改工作区文件，然后重新读取验证", events=events, actual_outcome="completed",
        mixed_risk_policy="partial",
    )
    assert task.qualification == "partial_evidence"
    connection = _connection(tmp_path)
    persist_task_evidence(connection, task)
    persist_task_evidence(connection, task)
    assert connection.execute("SELECT count(*) FROM evolution_task_evidence").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM evolution_step_evidence").fetchone()[0] == 3
    assert connection.execute(
        "SELECT count(*) FROM evolution_task_links WHERE task_id=?", (task.task_id,)
    ).fetchone()[0] == 1
    connection.close()


def test_r4_is_archive_only_and_not_candidate_eligible(tmp_path) -> None:
    event = ({"event_id": "e4", "tool_name": "exec", "status": "blocked", "detail": "outside workspace boundary"},)
    task = build_task_evidence(
        workspace=str(tmp_path.resolve()), session_key="session", trace_id="trace-r4", turn_id="turn-r4",
        user_text="请检查并处理这个文件", events=event, actual_outcome="blocked",
    )
    assert task.qualification == "unsafe_for_skill"
    assert task.steps[0].risk_level == "R4"
    assert task.steps[0].candidate_use == "archive_only"


def test_pure_read_records_are_groupable_for_m19(tmp_path) -> None:
    connection = _connection(tmp_path)
    workspace = str(tmp_path.resolve())
    for index in range(3):
        task = build_task_evidence(
            workspace=workspace, session_key="session", trace_id=f"trace-read-{index}", turn_id=f"turn-{index}",
            user_text="请查看工作区有哪些 Skill 并列出名称",
            events=({"event_id": f"e{index}", "tool_name": "skill_read", "status": "ok", "verification_kind": "read_success"},),
            actual_outcome="completed",
        )
        persist_task_evidence(connection, task)
    records = load_stepwise_candidate_records(connection, workspace)
    assert len(records) == 3
    assert len({record["task_key"] for record in records}) == 1
    connection.close()


def test_m19_new_and_existing_skill_candidates_are_isolated(tmp_path) -> None:
    connection = _connection(tmp_path)
    workspace = str(tmp_path.resolve())
    task = build_task_evidence(
        workspace=workspace, session_key="session", trace_id="trace-candidate", turn_id="turn-candidate",
        user_text="请查看工作区有哪些 Skill 并列出名称",
        events=({"event_id": "candidate-event", "tool_name": "skill_read", "status": "ok", "verification_kind": "read_success"},),
        actual_outcome="completed",
    )
    persist_task_evidence(connection, task)
    new_candidate = stage_task_candidate(connection, task=task, workspace=workspace)
    assert new_candidate.source_kind == "skill_candidate"
    assert get_staged_candidate(connection, new_candidate.candidate_id) == new_candidate

    connection.execute(
        "INSERT INTO skills(skill_id,name,source_kind,current_revision_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("skill-existing", "workspace-lookup", "workspace", "rev-existing", "now", "now"),
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) VALUES(?,?,?,?,?,?,?)",
        ("rev-existing", "skill-existing", "1", "sha256:baseline", "# baseline", "test", "now"),
    )
    connection.commit()
    revision = stage_task_candidate(
        connection, task=task, workspace=workspace, matched_skill_id="skill-existing",
        matched_skill_name="workspace-lookup",
    )
    assert revision.source_kind == "skill_revision_candidate"
    assert revision.baseline_revision_id == "rev-existing"
    assert connection.execute(
        "SELECT current_revision_id FROM skills WHERE skill_id='skill-existing'"
    ).fetchone()[0] == "rev-existing"
    uncertain = stage_task_candidate(
        connection, task=task, workspace=workspace, matched_skill_id="skill-existing",
        matched_skill_name="workspace-lookup", match_confidence=0.6,
    )
    assert uncertain.status == "manual_review_required"
    assert list_staged_candidates(connection, workspace=workspace)
    connection.close()


def test_agent_loop_shadow_capture_keeps_legacy_and_stepwise_records(tmp_path) -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = str(tmp_path)
    loop.phase6_config = SimpleNamespace(
        enabled=True,
        evolution=SimpleNamespace(
            stepwise_evidence_enabled=True,
            stepwise_evidence_mode="shadow",
            mixed_risk_policy="manual",
        ),
    )
    context = SimpleNamespace(
        kind=TurnKind.USER,
        original_user_text="请读取工作区 README.md 并告诉我是否存在",
        audit_turn=SimpleNamespace(trace_id="trace-agent-stepwise", turn_id="turn-agent-stepwise"),
        audit_run=SimpleNamespace(source_type="user"),
        stop_reason="completed",
        request_context=None,
        session_key="session-agent-stepwise",
        tools_used=["read_file"],
        tool_events=[],
        tool_evidence=[
            {
                "event_id": "event-agent-stepwise",
                "tool_name": "read_file",
                "operation_kind": "file_read",
                "safe_input_summary": "path=README.md",
                "resource_key": "sha256:resource",
                "verification_kind": "read_success",
                "status": "ok",
            }
        ],
    )
    loop._capture_semantic_evolution_evidence(context)
    connection = _connection(tmp_path)
    row = connection.execute(
        "SELECT qualification,verification_status FROM evolution_task_evidence WHERE task_id=?",
        ("evolution-task:trace-agent-stepwise",),
    ).fetchone()
    assert tuple(row) == ("candidate_eligible", "verified")
    step = connection.execute(
        "SELECT tool_name,risk_level,resource_key,verification_kind FROM evolution_step_evidence"
    ).fetchone()
    assert tuple(step) == ("read_file", "R0", "sha256:resource", "read_success")
    assert connection.execute(
        "SELECT evidence_status FROM semantic_task_evidence WHERE trace_id=?",
        ("trace-agent-stepwise",),
    ).fetchone()[0] == "eligible"
    connection.close()


def test_enforced_stepwise_scan_stages_before_existing_m15_gate(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = _connection(workspace)
    workspace_value = str(workspace.resolve())
    for index in range(3):
        task = build_task_evidence(
            workspace=workspace_value, session_key="session", trace_id=f"trace-enforced-{index}",
            turn_id=f"turn-enforced-{index}", user_text="请查看工作区有哪些 Skill 并列出名称",
            events=({"event_id": f"event-enforced-{index}", "tool_name": "skill_read", "status": "ok", "verification_kind": "read_success"},),
            actual_outcome="completed",
        )
        persist_task_evidence(connection, task)
    connection.close()

    from nanobot.config.schema import Phase6Config, Phase6EvolutionConfig

    result = run_phase6_review_scan(
        workspace,
        Phase6Config(
            enabled=True,
            min_repeat_count=3,
            evolution=Phase6EvolutionConfig(
                stepwise_evidence_enabled=True,
                stepwise_evidence_mode="enforced",
                stepwise_candidate_enabled=True,
                ab_evaluation_mode="disabled",
            ),
        ),
    )
    assert result.status == "completed"
    connection = _connection(workspace)
    assert connection.execute("SELECT count(*) FROM evolution_candidate_staging").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 1
    connection.close()


def test_enforced_mixed_failure_still_enters_recovery_evidence(tmp_path) -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = str(tmp_path)
    loop.phase6_config = SimpleNamespace(
        enabled=True,
        evolution=SimpleNamespace(
            stepwise_evidence_enabled=True,
            stepwise_evidence_mode="enforced",
            mixed_risk_policy="manual",
        ),
    )
    context = SimpleNamespace(
        kind=TurnKind.USER,
        original_user_text="请读取不存在的 README.md",
        audit_turn=SimpleNamespace(trace_id="trace-enforced-failure", turn_id="turn-enforced-failure"),
        audit_run=SimpleNamespace(source_type="user"),
        stop_reason="completed",
        request_context=None,
        session_key="session-enforced-failure",
        tools_used=[],
        tool_events=[],
        tool_evidence=[
            {"event_id": "event-enforced-failure", "tool_name": "read_file", "status": "error",
             "error_code": "file_not_found", "error_type": "FileNotFoundError",
             "safe_input_summary": "path=README.md", "verification_kind": "read_success"},
        ],
    )
    loop._capture_semantic_evolution_evidence(context)
    connection = _connection(tmp_path)
    assert tuple(connection.execute(
        "SELECT status,failure_family FROM recovery_episodes WHERE failure_trace_id=?",
        ("trace-enforced-failure",),
    ).fetchone()) == ("failed", "resource_not_found")
    assert connection.execute(
        "SELECT qualification FROM evolution_task_evidence WHERE task_id=?",
        ("evolution-task:trace-enforced-failure",),
    ).fetchone()[0] == "partial_evidence"
    connection.close()
