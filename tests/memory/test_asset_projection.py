"""Fresh L1 projection, four-state facts, source correction and payload boundaries."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from nanobot.agent.loop import AgentLoop, TurnKind
from nanobot.config.schema import Phase6EvolutionConfig
from nanobot.memory.asset_projection import project_task_asset
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.stepwise_evidence import build_task_evidence, persist_task_evidence
from nanobot.memory.task_outcome import resolve_task_outcome


def _capture(workspace, *, trace="trace", session="session", stop="completed", events=None, goal=None):
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = str(workspace)
    loop.phase6_config = SimpleNamespace(enabled=True, evolution=Phase6EvolutionConfig(
        asset_layer_enabled=True, asset_retrieval_enabled=False,
        stepwise_evidence_enabled=True, stepwise_evidence_mode="enforced",
    ))
    events = events if events is not None else [{"tool_name":"read_file", "status":"ok", "resource_class":"workspace-doc"}]
    context = SimpleNamespace(
        kind=TurnKind.USER, original_user_text=goal or "请读取工作区文档并概括其用途",
        audit_turn=SimpleNamespace(trace_id=trace, turn_id="turn:"+trace),
        audit_run=SimpleNamespace(source_type="user"), stop_reason=stop,
        request_context=None, session_key=session, tools_used=[e["tool_name"] for e in events],
        tool_evidence=events, tool_events=[],
    )
    loop._capture_semantic_evolution_evidence(context)
    return loop, context


@pytest.mark.parametrize("recovery,stop,statuses,expected", [
    ("recovered","error",["error"],"recovered"), ("failed","completed",["ok"],"failure"),
    ("insufficient_recovery_evidence","error",[],"failure"), (None,"tool_error",[],"failure"),
    (None,"completed",["ok","error"],"partial"), (None,"max_iterations",["ok"],"partial"),
    (None,"empty_final_response",[],"partial"), (None,"completed",["ok"],"success"),
    (None,"completed",[],None), (None,"unknown",["ok"],None),
])
def test_one_outcome_resolver_uses_ordered_facts(recovery, stop, statuses, expected):
    assert resolve_task_outcome(recovery_status=recovery,stop_reason=stop,steps=[{"status":s} for s in statuses]) == expected


def test_success_shadow_asset_has_complete_steps_and_stable_source(tmp_path):
    loop, ctx = _capture(tmp_path)
    loop._capture_semantic_evolution_evidence(ctx)
    connection = open_maintenance_db(tmp_path)
    asset = dict(connection.execute("SELECT * FROM memory_assets").fetchone())
    assert asset["asset_level"] == "L1" and asset["outcome"] == "success"
    assert asset["expires_at"] is None and asset["confidence_source"] is None
    assert asset["origin_table"] == "evolution_task_evidence" and asset["origin_id"] == "evolution-task:trace"
    assert asset["trigger_condition"] and asset["visibility"] == "shared"
    steps = json.loads(asset["steps_json"])
    assert set(steps[0]) == {"seq","operation_family","resource_class","tool_category","status","risk_level",
                            "redaction_level","input_summary","output_summary","error_code","error_type","error_source",
                            "error_class","retryability","verification_kind"}
    assert steps[0]["tool_category"] == "filesystem-read"
    assert steps[0]["resource_class"] == "workspace-doc"
    assert connection.execute("SELECT count(*) FROM memory_assets").fetchone()[0] == 1
    assert connection.execute("SELECT actual_outcome FROM evolution_task_evidence").fetchone()[0] == "success"
    assert connection.execute("SELECT count(*) FROM skills WHERE status='active'").fetchone()[0] == 0


@pytest.mark.parametrize("tool,risk,level",[("read_file","R0","normal"),("web_fetch","R1","normal"),
                                            ("write_file","R2","abstract"),("exec","R3","maximal")])
def test_risk_levels_record_categories_without_replay_payload(tmp_path,tool,risk,level):
    event={"tool_name":tool,"status":"ok","resource_class":"workspace-doc",
           "safe_input_summary":"full command or file contents must not reach assets"}
    _capture(tmp_path,events=[event])
    connection=open_maintenance_db(tmp_path)
    asset=connection.execute("SELECT * FROM memory_assets").fetchone()
    assert asset is not None and asset["redaction_level"] == level
    step=json.loads(asset["steps_json"])[0]
    assert step["risk_level"] == risk
    if risk in {"R2","R3"}:
        assert step["input_summary"] is None and step["output_summary"] is None
    assert "full command" not in asset["steps_json"]


def test_r4_boundary_step_is_recorded_without_summary_or_command(tmp_path):
    _capture(tmp_path,events=[{"tool_name":"exec","status":"blocked","detail":"outside_workspace",
                              "error_code":"policy_blocked","error_type":"PolicyError","retryability":"non_retryable"}])
    connection=open_maintenance_db(tmp_path)
    asset=connection.execute("SELECT * FROM memory_assets").fetchone()
    assert asset is not None and asset["redaction_level"] == "maximal"
    step=json.loads(asset["steps_json"])[0]
    assert step["input_summary"] is None and step["error_class"] == "policy-blocked"
    assert step["retryability"] == "non_retryable"


def test_correction_refreshes_original_failure_instead_of_creating_duplicate(tmp_path):
    event={"tool_name":"read_file","status":"error","error_code":"file_not_found",
           "error_type":"FileNotFoundError","error_source":"tool_result","retryability":"unknown",
           "resource_class":"workspace-doc"}
    loop, old = _capture(tmp_path,trace="failure",events=[event])
    connection=open_maintenance_db(tmp_path)
    original=connection.execute("SELECT asset_id FROM memory_assets").fetchone()[0]
    assert connection.execute("SELECT actual_outcome FROM evolution_task_evidence").fetchone()[0] == "failure"
    _capture(tmp_path,trace="correction",goal="请重新读取工作区文档并概括其用途")
    # A repeated original callback must not revert recovered facts.
    loop._capture_semantic_evolution_evidence(old)
    rows=list(connection.execute("SELECT * FROM memory_assets ORDER BY origin_id"))
    assert len(rows) == 2
    failure=next(row for row in rows if row["asset_id"]==original)
    assert failure["outcome"] == "recovered"
    assert set(json.loads(failure["source_trace_ids"])) == {"failure","correction"}
    assert connection.execute("SELECT status FROM recovery_episodes").fetchone()[0] == "recovered"
    assert connection.execute("SELECT qualification FROM evolution_task_evidence WHERE task_id='evolution-task:failure'").fetchone()[0] == "manual_review_required"


def test_high_risk_failure_is_not_lost_at_legacy_early_return(tmp_path):
    _capture(tmp_path,stop="tool_error",events=[{"tool_name":"write_file","status":"error",
                                               "error_code":"permission_denied","resource_class":"workspace-doc"}])
    connection=open_maintenance_db(tmp_path)
    assert tuple(connection.execute("SELECT status,operation_family FROM recovery_episodes").fetchone()) == ("failed","file_write")
    assert tuple(connection.execute("SELECT outcome,error_class FROM memory_assets").fetchone()) == ("failure","permission-denied")


@pytest.mark.parametrize("goal",["请查询 event_count 和 turn_started 事件统计", "请查看 user_id=private-member 的文档",
                                  "请检查 ```bash\ncurl example\n``` 输出"])
def test_forbidden_goal_never_becomes_asset(tmp_path,goal):
    _capture(tmp_path,goal=goal)
    connection=open_maintenance_db(tmp_path)
    assert connection.execute("SELECT count(*) FROM memory_assets").fetchone()[0] == 0


def test_legacy_completed_value_is_not_a_projection_outcome(tmp_path):
    connection=open_maintenance_db(tmp_path)
    task=build_task_evidence(workspace=str(tmp_path),session_key="session",trace_id="legacy",turn_id="turn",
        user_text="请读取工作区文档内容",events=[{"tool_name":"read_file","status":"ok"}],actual_outcome="completed")
    persist_task_evidence(connection,task)
    assert project_task_asset(connection,task.task_id) is None


def test_six_documents_group_by_category_and_unknown_error_stays_unclassified(tmp_path):
    for index in range(6):
        _capture(tmp_path,trace=str(index),goal=f"请读取文档 {index}.md 并概括用途")
    connection=open_maintenance_db(tmp_path)
    assert connection.execute("SELECT count(DISTINCT family_key) FROM memory_assets").fetchone()[0] == 1
    _capture(tmp_path,trace="unknown",events=[{"tool_name":"read_file","status":"error","retryability":"unknown"}])
    asset=connection.execute("SELECT * FROM memory_assets WHERE origin_id='evolution-task:unknown'").fetchone()
    assert asset["error_class"] is None
    assert json.loads(asset["steps_json"])[0]["error_class"] is None


def test_l1_and_l2_database_constraints_reject_invalid_shapes(tmp_path):
    _capture(tmp_path)
    connection=open_maintenance_db(tmp_path)
    for assignment in ("confidence_source='inferred'", "task_goal=''", "family_key=''"):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE memory_assets SET "+assignment)
        connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE memory_assets SET asset_level='L2',confidence_source='inferred'")
    connection.rollback()


def test_redacted_goal_never_persists_credentials(tmp_path):
    _capture(tmp_path,goal="请读取 token=secret-value 指定的文件")
    connection=open_maintenance_db(tmp_path)
    asset=connection.execute("SELECT * FROM memory_assets").fetchone()
    assert asset is not None
    assert "secret-value" not in str(dict(asset))
    assert "token=" not in asset["task_goal"]


def test_new_correction_does_not_backfill_unprojected_legacy_task(tmp_path):
    _capture(tmp_path,trace="old-failure",events=[{"tool_name":"read_file","status":"error","error_code":"file_not_found"}])
    connection=open_maintenance_db(tmp_path)
    connection.execute("DELETE FROM memory_assets")  # Construct a pre-asset-layer source, only in test SQLite.
    connection.commit()
    _capture(tmp_path,trace="new-correction",goal="请重新读取工作区文档并概括其用途")
    assert connection.execute("SELECT count(*) FROM memory_assets").fetchone()[0] == 1
    assert connection.execute("SELECT origin_id FROM memory_assets").fetchone()[0] == "evolution-task:new-correction"


def test_group_provenance_is_stable_without_retaining_group_openid(tmp_path):
    loop, ctx = _capture(tmp_path,trace="before-group")
    ctx.audit_turn.trace_id="group-task"
    ctx.msg=SimpleNamespace(channel="qq",chat_id="private-group-value",metadata={"group_openid":"private-group-value"})
    loop._capture_semantic_evolution_evidence(ctx)
    connection=open_maintenance_db(tmp_path)
    group=connection.execute("SELECT source_group FROM memory_assets WHERE origin_id='evolution-task:group-task'").fetchone()[0]
    assert group.startswith("qq:") and "private-group-value" not in group
