from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from nanobot.config.schema import Config, Phase6EvolutionConfig
from nanobot.memory.continuous import (
    EvidenceRecord,
    Phase6Disabled,
    Phase6RuntimeConfig,
    Phase6State,
    append_evidence,
    build_versioned_evalpack,
    collect_capacity_metrics,
    collect_governance_metrics,
    govern_workspace,
    load_state,
    make_proposal,
    pause_and_rollback,
    persist_proposal,
    phase6_active,
    resume_after_review,
    rollback_skill_revision,
    run_cycle,
    save_state,
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
    with pytest.raises(ValueError):
        resume_after_review(tmp_path, _cfg(), actor="reviewer", reason=" ")
    resumed = resume_after_review(tmp_path, _cfg(), actor="reviewer", reason="checked evidence")
    assert resumed.paused is False
    assert load_state(tmp_path).generation == 2
    evidence = (tmp_path / ".nanobot" / "phase6" / "evidence.jsonl").read_text(encoding="utf-8")
    assert "health_check" in evidence and "resume" in evidence


def test_governance_metrics_use_live_tables_and_persist_workspace_asset_baseline(tmp_path):
    workspace, connection = _db(tmp_path)
    from tests.memory.test_asset_retrieval import _asset

    _asset(connection, workspace, "baseline")
    connection.execute(
        "INSERT INTO phase6_alert_deliveries(alert_id,workspace,group_openid,category,fingerprint,payload,status,"
        "next_attempt_at,created_at,updated_at) VALUES('dead',?,'g','scan','f','safe','dead_letter','now','now','now')",
        (str(workspace.resolve()),),
    )
    connection.execute(
        "INSERT INTO phase6_alert_deliveries(alert_id,workspace,group_openid,category,fingerprint,payload,status,"
        "next_attempt_at,created_at,updated_at) VALUES('foreign-dead','other','g','scan','ff','safe','dead_letter','now','now','now')"
    )
    connection.commit()
    metrics = collect_governance_metrics(connection, workspace=str(workspace.resolve()), baseline_assets=None)
    assert metrics["asset_baseline_count"] == 1 and metrics["memory_growth_ratio"] == 0
    assert metrics["dead_letter_count"] == 1 and metrics["unauthorized_attempts"] == 0
    assert metrics["notification_failure_rate"] == 1.0 and metrics["notifications"] == 1
    state, rollbacks = govern_workspace(connection, workspace, _cfg(dead_letter_pause_threshold=1), metrics)
    assert state.paused and "dead_letter_threshold" in state.pause_reason
    assert state.baseline_assets == 1 and not rollbacks
    assert load_state(workspace).paused
    connection.close()


def test_governance_counts_only_policy_block_events_from_the_read_only_audit_index(tmp_path):
    workspace, connection = _db(tmp_path)
    audit_root = tmp_path / "audit"
    (audit_root / "state").mkdir(parents=True)
    index = sqlite3.connect(audit_root / "state/audit-index.sqlite")
    index.execute("CREATE TABLE events(event_type TEXT NOT NULL, occurred_at TEXT NOT NULL)")
    since = datetime.now(UTC).isoformat(timespec="microseconds")
    index.executemany("INSERT INTO events VALUES(?,?)", [("policy_blocked", since), ("tool_finished", since)])
    index.commit()
    index.close()
    metrics = collect_governance_metrics(
        connection, workspace=str(workspace.resolve()), baseline_assets=None, audit_root=audit_root,
    )
    assert metrics["unauthorized_attempts"] == 1
    assert metrics["permission_denials"] == 0
    assert metrics["asset_baseline_count"] == 1
    assert connection.execute("SELECT count(*) FROM memory_assets").fetchone()[0] == 0
    connection.close()


def test_governance_auto_rollback_is_cas_scoped_to_adopted_workspace_skills(tmp_path):
    from nanobot.memory.skill_adoption import adopt_workspace_proposal
    from tests.memory.test_skill_adoption import _config, _setup

    # Use an actually adopted file, not a database-only pointer fixture.
    workspace, connection, _ = _setup(tmp_path)
    assert adopt_workspace_proposal(
        connection, workspace, "prop-adopt-test", actor="admin", config=_config(),
    ).status == "adopted"
    state, rollbacks = govern_workspace(connection, workspace, _cfg(), {"regression_failures": 3})
    assert state.paused and rollbacks == (("prop-adopt-test", True),)
    assert (workspace / "skills/demo/SKILL.md").read_text() == "# Demo\n\nold\n"
    assert connection.execute("SELECT current_revision_id FROM skills").fetchone()[0] == "base-1"
    assert connection.execute("SELECT status FROM skill_proposals").fetchone()[0] == "rolled_back"
    assert connection.execute("SELECT action FROM proposal_actions ORDER BY rowid DESC").fetchone()[0] == "governance_rollback"
    assert govern_workspace(connection, workspace, _cfg(), {"regression_failures": 3})[1] == ()
    connection.close()


def test_review_cycle_does_not_select_candidates_after_workspace_pause(tmp_path):
    workspace, connection = _db(tmp_path)
    save_state(workspace, Phase6State(paused=True, pause_reason="permission_denial", baseline_assets=0))
    result = run_cycle(connection, workspace, _cfg(), trace_records=[{"summary": "repeat", "trace_id": "t", "outcome": "success"}] * 3)
    assert result.status == "paused" and result.selected_tasks == ()
    connection.close()


@pytest.mark.asyncio
async def test_production_review_governance_pause_returns_before_candidate_evaluation(tmp_path, monkeypatch):
    from nanobot.memory import phase6_trigger
    from nanobot.memory.evolution_worker import worker_tick
    from nanobot.memory.work_queue import enqueue_work

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = type("WorkerConfig", (), {
        "workspace_path": workspace,
        "phase6": type("Phase6", (), {
            "enabled": True,
            "kill_switch": False,
            "evolution": Phase6EvolutionConfig(),
        })(),
    })()
    connection = open_maintenance_db(workspace)
    enqueue_work(connection, str(workspace.resolve()), "review", "paused-review")
    connection.close()
    observed = {"cycle": False}

    monkeypatch.setattr(
        phase6_trigger,
        "collect_governance_metrics",
        lambda *_args, **_kwargs: {"regression_failures": 3},
    )

    original_cycle = phase6_trigger.run_cycle

    def paused_cycle(*args, **kwargs):
        observed["cycle"] = True
        assert load_state(workspace).paused
        return original_cycle(*args, **kwargs)

    monkeypatch.setattr(phase6_trigger, "run_cycle", paused_cycle)
    result = await phase6_trigger.run_phase6_review_scan_async(workspace, config.phase6)
    assert result.status == "paused" and not observed["cycle"]
    worker_result = await worker_tick(config, owner="paused-worker")
    assert worker_result["status"] == "paused"
    connection = open_maintenance_db(workspace)
    assert tuple(connection.execute("SELECT status,attempt_count FROM evolution_work_items").fetchone()) == ("queued", 0)
    assert load_state(workspace).paused
    connection.close()


def test_resume_preserves_first_capacity_baseline(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    save_state(workspace, Phase6State(paused=True, pause_reason="reviewed", generation=2, baseline_assets=17))
    resumed = resume_after_review(workspace, _cfg(), actor="reviewer", reason="verified the adopted revision")
    assert not resumed.paused and resumed.generation == 3 and resumed.baseline_assets == 17


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


@pytest.mark.parametrize("change", ["manual_edit", "new_pointer", "outside_path", "write_failure"])
def test_governance_rollback_preserves_changes_and_recovers_failed_file_write(tmp_path, monkeypatch, change):
    from nanobot.memory import skill_adoption
    from tests.memory.test_skill_adoption import _config, _setup

    workspace, connection, _ = _setup(tmp_path)
    assert skill_adoption.adopt_workspace_proposal(
        connection, workspace, "prop-adopt-test", actor="admin", config=_config(),
    ).status == "adopted"
    path = workspace / "skills/demo/SKILL.md"
    expected = path.read_text()
    pointer = "candidate-1"
    if change == "manual_edit":
        expected = "user changed this file"
        path.write_text(expected)
    elif change == "new_pointer":
        pointer = "base-1"
        connection.execute("UPDATE skills SET current_revision_id=?", (pointer,))
        connection.commit()
    elif change == "outside_path":
        outside = tmp_path / "protected.md"
        outside.write_text(expected)
        path.unlink()
        path.symlink_to(outside)
    elif change == "write_failure":
        original = skill_adoption._atomic_write

        def fail_after_replace(*args):
            original(*args)
            raise OSError("disk failure after replace")

        monkeypatch.setattr(skill_adoption, "_atomic_write", fail_after_replace)
    state, rollbacks = govern_workspace(connection, workspace, _cfg(), {"regression_failures": 3})
    assert state.paused and not any(applied for _, applied in rollbacks)
    assert path.read_text() == expected
    assert connection.execute("SELECT current_revision_id FROM skills").fetchone()[0] == pointer
    assert connection.execute("SELECT status FROM skill_proposals").fetchone()[0] == "adopted"
    assert not connection.execute("SELECT 1 FROM proposal_actions WHERE action='governance_rollback'").fetchone()
    connection.close()


def test_capacity_limit_pauses_even_without_asset_growth(tmp_path):
    state = update_health(tmp_path, {"capacity_exceeded": True, "memory_growth_ratio": 0}, _cfg())
    assert state.paused and state.pause_reason == "memory_capacity"


def test_cross_process_health_updates_preserve_every_generation(tmp_path):
    import subprocess
    import sys

    code = (
        "import sys; from nanobot.memory.continuous import update_health, Phase6RuntimeConfig; "
        "[update_health(sys.argv[1], {}, Phase6RuntimeConfig(enabled=True)) for _ in range(6)]"
    )
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)]) for _ in range(3)]
    assert all(process.wait(timeout=30) == 0 for process in processes)
    assert load_state(tmp_path).generation == 18



def test_governance_permission_and_action_counts_exclude_other_workspaces(tmp_path):
    from tests.memory.test_skill_adoption import _setup

    workspace, connection, _ = _setup(tmp_path)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    for scope in (str(workspace.resolve()), "foreign-workspace"):
        connection.execute(
            "INSERT INTO evolution_task_evidence(task_id,workspace,qualification,redaction_status,created_at,updated_at) "
            "VALUES(?,?,'manual_review_required','safe',?,?)", (scope, scope, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO evolution_step_evidence(step_id,task_id,workspace,sequence_no,tool_name,status,"
            "failure_family,risk_level,candidate_use,redaction_status,created_at) "
            "VALUES(?,?,?,0,'read_file','error','permission_denied','R0','archive_only','safe',?)",
            (scope, scope, scope, stamp),
        )
        connection.execute(
            "INSERT INTO proposal_actions(action_id,proposal_id,workspace,action,actor_openid,idempotency_key,"
            "request_digest,result_status,created_at) VALUES(?,'prop-adopt-test',?,'approve','tester',?,"
            "'safe','forbidden',?)", (scope, scope, scope, stamp),
        )
    connection.commit()
    metrics = collect_governance_metrics(connection, workspace=str(workspace.resolve()), baseline_assets=None)
    assert metrics["permission_denials"] == metrics["unauthorized_attempts"] == 1
    connection.close()
