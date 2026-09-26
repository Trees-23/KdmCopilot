from __future__ import annotations

from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.memory.evolution_orchestrator import CandidateSpec, run_fixture_review_cycle
from nanobot.memory.lock import acquire_lock
from nanobot.memory.maintenance import open_maintenance_db


def _setup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = open_maintenance_db(workspace)
    connection.execute(
        "INSERT INTO skills(skill_id,name,source_kind,created_at,updated_at) VALUES('skill-1','demo','workspace','now','now')"
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES('base-1','skill-1','1','sha256:base','old','test','now')"
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES('candidate-1','skill-1','2','sha256:candidate','new','test','now')"
    )
    connection.commit()
    return workspace, connection


def _spec():
    cases = tuple(
        {"case_id": f"case-{index:02d}", "prompt": f"fixture prompt {index}", "expected": f"answer {index}"}
        for index in range(20)
    )
    return CandidateSpec(
        task_key="repeat fixture",
        skill_id="skill-1",
        skill_name="demo",
        source_kind="workspace",
        baseline_revision_id="base-1",
        candidate_revision_id="candidate-1",
        baseline_hash="sha256:base",
        candidate_hash="sha256:candidate",
        cases=cases,
        fixture_hash="sha256:fixture",
    )


def test_phase6_off_does_not_scan_or_write_proposals(tmp_path):
    workspace, connection = _setup(tmp_path)
    result = run_fixture_review_cycle(
        connection,
        workspace,
        Phase6RuntimeConfig(),
        trace_records=[{"trace_id": "t1", "summary": "repeat fixture", "outcome": "success"}],
    )
    assert result.status == "disabled"
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0


def test_fixture_cycle_connects_trace_to_eval_gate_and_proposal(tmp_path):
    workspace, connection = _setup(tmp_path)
    config = Phase6RuntimeConfig(enabled=True, max_candidates_per_cycle=1)
    result = run_fixture_review_cycle(
        connection,
        workspace,
        config,
        trace_records=[
            {"trace_id": "trace-1", "summary": "repeat fixture", "outcome": "success"},
            {"trace_id": "trace-2", "summary": "repeat fixture", "outcome": "success"},
        ],
        failed_traces=[{"trace_id": "failed-1", "outcome": "failed", "summary": "fixture failure"}],
        candidate_specs=[_spec()],
    )
    assert result.status == "completed"
    assert result.selected_count == 1
    assert len(result.eval_run_ids) == 1
    assert len(result.proposal_ids) == 1
    assert result.staged_case_ids == ("case:failed-1",)
    assert connection.execute("SELECT status FROM eval_runs").fetchone()[0] == "completed"
    assert connection.execute("SELECT gate_result FROM eval_runs").fetchone()[0] == "eligible_for_confirmation"
    assert connection.execute("SELECT status FROM skill_proposals").fetchone()[0] == "eligible_for_confirmation"
    assert connection.execute("SELECT current_revision_id FROM skills WHERE skill_id='skill-1'").fetchone()[0] is None
    assert (workspace / ".nanobot" / "phase6" / "evidence.jsonl").exists()


def test_fixture_cycle_respects_workspace_lease(tmp_path):
    workspace, connection = _setup(tmp_path)
    other = open_maintenance_db(workspace)
    lease = acquire_lock(connection, str(workspace.resolve()), "other-worker", lease_seconds=120)
    assert lease is not None
    try:
        result = run_fixture_review_cycle(
            other,
            workspace,
            Phase6RuntimeConfig(enabled=True),
            trace_records=[],
        )
        assert result.status == "locked"
    finally:
        connection.close()
        other.close()
