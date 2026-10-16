from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from nanobot.config.schema import Config
from nanobot.memory.continuous import (
    EvidenceRecord,
    Phase6Disabled,
    Phase6RuntimeConfig,
    append_evidence,
    build_versioned_evalpack,
    collect_capacity_metrics,
    load_state,
    make_proposal,
    pause_and_rollback,
    persist_proposal,
    phase6_active,
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
    with pytest.raises(Phase6Disabled):
        select_low_risk_tasks([], Phase6RuntimeConfig())
    assert phase6_active(_cfg(kill_switch=True)) is False
    runtime = Phase6RuntimeConfig.from_config(Config().phase6)
    assert runtime.enabled is False


def test_low_risk_selector_requires_repetition_and_excludes_sensitive_or_write_tasks() -> None:
    records = [
        {"trace_id": "t1", "summary": "List project status", "outcome": "succeeded", "tools": ["list_dir"]},
        {"trace_id": "t2", "summary": "  List   project status ", "outcome": "success", "tools": ["skill_read"]},
        {"trace_id": "t3", "summary": "List project status", "outcome": "success", "tools": ["read_file"]},
        {"trace_id": "t4", "summary": "List project status", "outcome": "failed", "tools": ["list_dir"]},
        {"trace_id": "t5", "summary": "read token: abc", "outcome": "success", "tools": ["read_file"]},
        {"trace_id": "t6", "summary": "List project status", "outcome": "success", "tools": ["write_file"]},
    ]
    candidates = select_low_risk_tasks(records, _cfg())
    assert len(candidates) == 1
    assert candidates[0].trace_ids == ("t1", "t2", "t3")
    assert candidates[0].frequency == 3


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


def test_proposal_is_traceable_and_rollback_is_explicit_cas(tmp_path) -> None:
    proposal = make_proposal(
        source_kind="shared", trace_ids=["t"], case_ids=["c"], eval_run_ids=["run"], config=_cfg(),
    )
    assert proposal.target == "git_pr_proposal"
    with pytest.raises(ValueError):
        make_proposal(source_kind="workspace", trace_ids=["t"], case_ids=[], eval_run_ids=["run"], config=_cfg())

    workspace, connection = _db(tmp_path)
    persisted = persist_proposal(
        connection,
        proposal,
        workspace=str(workspace),
        skill_name="demo",
        source_kind="shared",
        baseline_hash="sha256:base",
        candidate_hash="sha256:candidate",
    )
    assert persisted.proposal_id == proposal.proposal_id
    assert tuple(connection.execute(
        "SELECT status,target FROM skill_proposals WHERE proposal_id=?", (proposal.proposal_id,)
    ).fetchone()) == ("eligible_for_confirmation", "git_pr_proposal")
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
            {"trace_id": "ok-3", "summary": "repeat task", "outcome": "success"},
        ],
    )
    assert result.status == "completed"
    assert result.selected_tasks[0].frequency == 3
    assert connection.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM wiki_pages").fetchone()[0] == 0
    assert (workspace / ".nanobot" / "phase6" / "evidence.jsonl").exists()
    assert run_cycle(connection, workspace, _cfg(kill_switch=True), trace_records=[]) .status == "disabled"
    connection.close()


def test_capacity_alert_and_regression_pause_roll_back_current(tmp_path) -> None:
    workspace, connection = _db(tmp_path)
    _skill(connection)
    metrics = collect_capacity_metrics(connection, baseline_assets=1, max_assets=0)
    assert metrics["capacity_exceeded"] is True
    state, rolled_back = pause_and_rollback(
        connection, workspace, {"regression_failures": 3}, _cfg(), skill_id="s",
        baseline_revision_id="r1", expected_current_revision_id="r2",
    )
    assert state.paused is True and rolled_back is True
    assert connection.execute("SELECT current_revision_id FROM skills WHERE skill_id='s'").fetchone()[0] == "r1"
    connection.close()
