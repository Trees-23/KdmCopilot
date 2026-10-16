from __future__ import annotations

import pytest

from nanobot.memory.derivation import (
    build_eval_pack_draft,
    classify_outcome,
    seal_eval_pack,
)
from nanobot.memory.evaluation import (
    ReplayEvidence,
    create_eval_run,
    evaluate_gate,
    finish_eval_run,
    mark_stale_if_identity_changed,
    record_case_result,
    replay_cases,
    replay_consistency,
)
from nanobot.memory.maintenance import open_maintenance_db


def _db(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, open_maintenance_db(workspace)


def _skill(connection):
    connection.execute(
        """INSERT INTO skills(skill_id,namespace,name,source_kind,status,created_at,updated_at)
           VALUES('skill:test','workspace','test','workspace','active','now','now')"""
    )
    connection.execute(
        """INSERT INTO skill_revisions
           (revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at)
           VALUES('revision:test','skill:test','1','sha256:base','base','user','now')"""
    )
    connection.commit()


def test_outcome_classification_is_conservative():
    assert classify_outcome("succeeded") == "success"
    assert classify_outcome("timeout") == "partial"
    assert classify_outcome("denied") == "blocked"
    assert classify_outcome("future-status") == "unknown"


def test_eval_pack_split_boundaries_and_independent_seal(tmp_path):
    workspace, connection = _db(tmp_path)
    _skill(connection)
    cases = [{"case_id": f"case-{i}", "prompt": f"question {i}", "expected": "ok"} for i in range(20)]
    draft = build_eval_pack_draft(
        connection,
        skill_id="skill:test",
        skill_revision_id="revision:test",
        cases=cases,
        fixture_hash="sha256:fixture",
    )
    assert draft.evidence_grade == "standard"
    assert [case.split for case in draft.cases].count("train") == 10
    assert [case.split for case in draft.cases].count("validation") == 5
    assert [case.split for case in draft.cases].count("holdout") == 5
    with pytest.raises(ValueError):
        seal_eval_pack(connection, draft.eval_pack_id, reviewer="agent")
    assert seal_eval_pack(connection, draft.eval_pack_id, reviewer="reviewer-1")
    assert connection.execute("SELECT status FROM eval_packs WHERE eval_pack_id=?", (draft.eval_pack_id,)).fetchone()[0] == "sealed"
    connection.close()


def test_replay_results_gate_and_stale_identity(tmp_path):
    workspace, connection = _db(tmp_path)
    _skill(connection)
    draft = build_eval_pack_draft(
        connection,
        skill_id="skill:test", skill_revision_id="revision:test", fixture_hash="fixture",
        cases=[{"case_id": f"case-{i}", "prompt": str(i), "expected": "ok"} for i in range(20)],
    )
    run_id = create_eval_run(
        connection, eval_pack_id=draft.eval_pack_id, baseline_revision_id="revision:test",
        baseline_hash="base", candidate_revision_id=None, candidate_hash=None, model_id="model",
        tool_schema_digest="tools", fixture_hash="fixture", dataset_hash=draft.dataset_hash, seed="1",
    )
    for case in draft.cases:
        record_case_result(connection, eval_run_id=run_id, case=case,
                           evidence=ReplayEvidence("ok", tokens=10, latency_ms=5), judge_actor="judge")
    finish_eval_run(connection, run_id)
    gate = evaluate_gate(connection, eval_run_id=run_id)
    assert gate.eligible is True
    assert mark_stale_if_identity_changed(connection, eval_run_id=run_id, tool_schema_digest="changed",
                                          fixture_hash="fixture", dataset_hash=draft.dataset_hash)
    assert connection.execute("SELECT gate_result FROM eval_runs WHERE eval_run_id=?", (run_id,)).fetchone()[0] == "stale_baseline"
    connection.close()


def test_replay_cases_removes_fixture_after_failure(tmp_path):
    seen = []

    def replay(case, root):
        seen.append(root)
        (root / "fixture.txt").write_text(case.prompt)
        if case.case_key == "bad":
            raise RuntimeError("fixture failed")
        return ReplayEvidence("ok")

    with pytest.raises(RuntimeError):
        replay_cases((type("Case", (), {"case_key": "bad", "prompt": "x"})(),), replay)
    assert seen and not seen[0].exists()


def test_limited_replay_consistency_is_explicit():
    first = {"case": ReplayEvidence("ok", security_violation=False)}
    second = {"case": ReplayEvidence("ok", security_violation=False)}
    assert replay_consistency(first, second) == "consistent"
    assert replay_consistency(first, {"case": ReplayEvidence("different")}) == "inconsistent"
