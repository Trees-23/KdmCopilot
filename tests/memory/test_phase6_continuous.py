from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from nanobot.config.schema import Config, Phase6Config
from nanobot.memory.continuous import (
    EvidenceRecord,
    Phase6Disabled,
    Phase6RuntimeConfig,
    append_evidence,
    build_versioned_evalpack,
    collect_capacity_metrics,
    load_failed_trace_records,
    load_state,
    load_trace_records,
    make_proposal,
    pause_and_rollback,
    phase6_active,
    plan_retention,
    regression_case_from_trace,
    resume_after_review,
    rollback_skill_revision,
    run_cycle,
    select_low_risk_tasks,
    update_health,
)
from nanobot.memory.maintenance import open_maintenance_db


def _cfg(**kwargs) -> Phase6RuntimeConfig:
    return Phase6RuntimeConfig(enabled=True, **kwargs)


def _db(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, open_maintenance_db(workspace)


def _skill(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO skills(skill_id,name,source_kind,created_at,updated_at) VALUES('s','demo','workspace','now','now')"
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES('r1','s','1','sha256:r1','old','user','now')"
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES('r2','s','2','sha256:r2','new','user','now')"
    )
    connection.execute("UPDATE skills SET current_revision_id='r2',current_version='2' WHERE skill_id='s'")
    connection.commit()


def test_phase6_is_off_by_default_and_kill_switch_is_immediate() -> None:
    assert phase6_active(Phase6RuntimeConfig()) is False
    assert Config().phase6.enabled is False
    assert Phase6Config(payload_retention_days=7, retention_days=18).retention_days == 18
    with pytest.raises(ValueError):
        Phase6Config(payload_retention_days=19, retention_days=18)
    with pytest.raises(Phase6Disabled):
        select_low_risk_tasks([], Phase6RuntimeConfig())
    assert phase6_active(_cfg(kill_switch=True)) is False
    runtime = Phase6RuntimeConfig.from_config(Config().phase6)
    assert runtime.enabled is False


def test_low_risk_selector_requires_repetition_and_excludes_sensitive_or_write_tasks() -> None:
    records = [
        {"trace_id": "t1", "summary": "List project status", "outcome": "succeeded", "tools": ["list_dir"]},
        {"trace_id": "t2", "summary": "  List   project status ", "outcome": "success", "tools": ["skill_read"]},
        {"trace_id": "t3", "summary": "List project status", "outcome": "failed", "tools": ["list_dir"]},
        {"trace_id": "t4", "summary": "read token: abc", "outcome": "success", "tools": ["read_file"]},
        {"trace_id": "t5", "summary": "List project status", "outcome": "success", "tools": ["write_file"]},
    ]
    candidates = select_low_risk_tasks(records, _cfg())
    assert len(candidates) == 1
    assert candidates[0].trace_ids == ("t1", "t2")
    assert candidates[0].frequency == 2


def test_trace_index_loaders_remain_payload_free(tmp_path) -> None:
    _workspace, connection = _db(tmp_path)
    connection.execute(
        "INSERT INTO trace_index(trace_id,workspace,started_at,outcome,summary,event_count,tool_count,redaction_version,source_path,created_at,updated_at) "
        "VALUES('t1',?,'now','success','read only',1,0,'v1','derived://trace','now','now')",
        (str(tmp_path),),
    )
    connection.execute(
        "INSERT INTO trace_index(trace_id,workspace,started_at,outcome,summary,event_count,tool_count,redaction_version,source_path,created_at,updated_at) "
        "VALUES('t2',?,'now','failure','secret: hidden',1,1,'v1','derived://trace','now','now')",
        (str(tmp_path),),
    )
    connection.commit()
    assert len(load_trace_records(connection)) == 2
    assert load_failed_trace_records(connection)[0]["trace_id"] == "t2"
    assert "payload" not in load_trace_records(connection)[0]
    connection.close()


def test_failed_trace_becomes_redacted_staging_case_only(tmp_path) -> None:
    workspace, connection = _db(tmp_path)
    candidate = regression_case_from_trace(
        connection,
        {"trace_id": "trace-fail", "outcome": "timeout", "summary": "token: abc failed", "intent": "task"},
        _cfg(),
    )
    row = connection.execute("SELECT status,outcome_json FROM cases WHERE case_id=?", (candidate.case_id,)).fetchone()
    assert row[0] == "candidate"
    assert "[redacted]" in row[1]
    with pytest.raises(ValueError):
        regression_case_from_trace(connection, {"trace_id": "ok", "outcome": "success"}, _cfg())
    connection.close()


def test_versioned_evalpack_and_evidence_are_append_only_and_redacted(tmp_path) -> None:
    workspace, connection = _db(tmp_path)
    _skill(connection)
    first = build_versioned_evalpack(
        connection, skill_id="s", skill_revision_id="r2",
        cases=[{"case_id": "c1", "prompt": "one", "expected": "ok"}], fixture_hash="f1", config=_cfg(),
    )
    second = build_versioned_evalpack(
        connection, skill_id="s", skill_revision_id="r2",
        cases=[{"case_id": "c2", "prompt": "two", "expected": "ok"}], fixture_hash="f2", config=_cfg(),
    )
    assert first.eval_pack_id != second.eval_pack_id
    versions = [json.loads(row[0])["phase6_version"] for row in connection.execute(
        "SELECT split_policy_json FROM eval_packs ORDER BY created_at"
    )]
    assert versions == [1, 2]
    path = append_evidence(
        workspace,
        EvidenceRecord("proposal", datetime.now(UTC).isoformat(), trace_ids=("t1",), metrics={"token": "secret: abc"}),
        _cfg(),
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "secret: abc" not in lines[0]
    assert "[redacted]" in lines[0]
    connection.close()


def test_health_pauses_and_requires_independent_resume(tmp_path) -> None:
    state = update_health(
        tmp_path,
        {"regression_failures": 3, "permission_denials": 0, "fts_rebuild_failed": False},
        _cfg(),
    )
    assert state.paused is True
    assert state.pause_reason == "regression_threshold"
    with pytest.raises(ValueError):
        resume_after_review(tmp_path, _cfg(), actor="", reason="review")
    resumed = resume_after_review(tmp_path, _cfg(), actor="reviewer", reason="checked evidence")
    assert resumed.paused is False
    assert load_state(tmp_path).generation == 2
    evidence = (tmp_path / ".nanobot" / "phase6" / "evidence.jsonl").read_text(encoding="utf-8")
    assert "health_check" in evidence and "resume" in evidence


def test_retention_never_deletes_long_lived_summary(tmp_path) -> None:
    _workspace, connection = _db(tmp_path)
    now = datetime(2026, 2, 1, tzinfo=UTC)
    for trace_id, ended_at in (("recent", now - timedelta(days=1)), ("payload", now - timedelta(days=8)),
                               ("long", now - timedelta(days=20))):
        connection.execute(
            "INSERT INTO trace_index(trace_id,workspace,started_at,ended_at,outcome,summary,redaction_version,source_path,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (trace_id, str(tmp_path), ended_at.isoformat(), ended_at.isoformat(), "success", "long summary", "v1", "derived://trace", ended_at.isoformat(), ended_at.isoformat()),
        )
    connection.commit()
    plan = plan_retention(connection, now=now)
    assert plan.payload_expiry_trace_ids == ("long", "payload")
    assert plan.summary_review_trace_ids == ("long",)
    assert plan.protected_summary_trace_ids == ("long",)
    assert connection.execute("SELECT count(*) FROM trace_index").fetchone()[0] == 3
    connection.close()


def test_proposal_is_traceable_and_rollback_is_explicit_cas(tmp_path) -> None:
    proposal = make_proposal(
        source_kind="shared", trace_ids=["t"], case_ids=["c"], eval_run_ids=["run"], config=_cfg(),
    )
    assert proposal.target == "git_pr_proposal"
    with pytest.raises(ValueError):
        make_proposal(source_kind="workspace", trace_ids=["t"], case_ids=[], eval_run_ids=["run"], config=_cfg())

    _workspace, connection = _db(tmp_path)
    _skill(connection)
    assert rollback_skill_revision(
        connection, skill_id="s", target_revision_id="r1", expected_current_revision_id="r2",
        config=_cfg(), actor="reviewer", confirm=False,
    ) is False
    assert rollback_skill_revision(
        connection, skill_id="s", target_revision_id="r1", expected_current_revision_id="r2",
        config=_cfg(), actor="reviewer", confirm=True,
    ) is True
    assert connection.execute("SELECT current_revision_id FROM skills WHERE skill_id='s'").fetchone()[0] == "r1"
    assert rollback_skill_revision(
        connection, skill_id="s", target_revision_id="r2", expected_current_revision_id="r2",
        config=_cfg(), actor="reviewer", confirm=True,
    ) is False
    connection.close()


def test_cycle_is_bounded_and_failed_trace_case_is_staged(tmp_path) -> None:
    workspace, connection = _db(tmp_path)
    result = run_cycle(
        connection,
        workspace,
        _cfg(max_candidates_per_cycle=1),
        trace_records=[
            {"trace_id": "ok-1", "summary": "repeat task", "outcome": "success"},
            {"trace_id": "ok-2", "summary": "repeat task", "outcome": "success"},
        ],
        failed_traces=[{"trace_id": "bad-1", "outcome": "failed", "summary": "rejected"}],
    )
    assert result.status == "completed"
    assert result.selected_tasks[0].frequency == 2
    assert result.regression_case_ids == ("case:bad-1",)
    assert (workspace / ".nanobot" / "phase6" / "evidence.jsonl").exists()
    assert run_cycle(connection, workspace, _cfg(kill_switch=True), trace_records=[]) .status == "disabled"
    connection.close()


def test_capacity_alert_and_regression_pause_roll_back_current(tmp_path) -> None:
    workspace, connection = _db(tmp_path)
    _skill(connection)
    metrics = collect_capacity_metrics(connection, baseline_memory_records=1, max_memory_records=0)
    assert metrics["capacity_exceeded"] is True
    state, rolled_back = pause_and_rollback(
        connection, workspace, {"regression_failures": 3}, _cfg(), skill_id="s",
        baseline_revision_id="r1", expected_current_revision_id="r2",
    )
    assert state.paused is True and rolled_back is True
    assert connection.execute("SELECT current_revision_id FROM skills WHERE skill_id='s'").fetchone()[0] == "r1"
    connection.close()
