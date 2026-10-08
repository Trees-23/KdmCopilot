from __future__ import annotations

from dataclasses import replace

import pytest

from nanobot.memory.ab_evaluation import (
    ABResponse,
    _gate,
    _write_synthetic_fixture,
    evaluate_candidate_specs,
)
from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.memory.evolution_orchestrator import CandidateSpec
from nanobot.memory.maintenance import open_maintenance_db


def _spec() -> CandidateSpec:
    cases = tuple(
        {"case_id": f"case-{index}", "prompt": f"查询 Skill 清单 {index}", "expected": "结构化清单"}
        for index in range(10)
    )
    return CandidateSpec(
        task_key="semantic:quality", skill_id="skill:quality", skill_name="quality",
        source_kind="shared", baseline_revision_id="base", candidate_revision_id="candidate",
        baseline_hash="sha256:base", candidate_hash="sha256:candidate", cases=cases,
        fixture_hash="sha256:fixture", case_ids=tuple(item["case_id"] for item in cases),
        candidate_content="# 查询 Skill\n\n只读列出清单。",
    )


async def _executor(role: str, prompt: str, _spec: CandidateSpec) -> ABResponse:
    if role == "judge":
        if '"任务类别": "positive"' in prompt:
            return ABResponse(
                '{"baseline_score":0.70,"candidate_score":0.90,"candidate_consistent":true,'
                '"candidate_refused":false,"safety_clean":true}'
            )
        return ABResponse(
            '{"baseline_score":0.20,"candidate_score":1.0,"candidate_consistent":true,'
            '"candidate_refused":true,"safety_clean":true}'
        )
    return ABResponse("已完成", tokens=10, latency_ms=10)


async def test_ab_gate_runs_two_sides_and_enforces_daily_budget(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = open_maintenance_db(workspace)
    config = Phase6RuntimeConfig(
        enabled=True, ab_evaluation_mode="enforced", ab_evaluation_max_candidates_per_day=2,
        ab_evaluation_max_model_calls_per_day=40,
    )
    first = await evaluate_candidate_specs(connection, workspace, (_spec(),), config, executor=_executor)
    assert first.status == "completed"
    assert first.approved_specs == (_spec(),)
    assert first.model_calls_used == 20
    assert tuple(connection.execute("SELECT status,model_calls_used FROM ab_evaluations").fetchone()) == ("passed", 20)
    assert connection.execute("SELECT count(*) FROM ab_case_results").fetchone()[0] == 8

    second = await evaluate_candidate_specs(connection, workspace, (_spec(),), config, executor=_executor)
    assert second.status == "completed"
    assert second.model_calls_used == 20
    exhausted = await evaluate_candidate_specs(connection, workspace, (_spec(),), config, executor=_executor)
    assert exhausted.status == "budget_exhausted"
    assert exhausted.approved_specs == ()
    connection.close()


async def test_ab_gate_fails_closed_for_insufficient_real_evidence(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = open_maintenance_db(workspace)
    spec = _spec()
    insufficient = replace(spec, case_ids=spec.case_ids[:2])
    result = await evaluate_candidate_specs(
        connection, workspace, (insufficient,), Phase6RuntimeConfig(enabled=True), executor=_executor
    )
    assert result.approved_specs == ()
    assert tuple(connection.execute("SELECT status,reason_code FROM ab_evaluations").fetchone()) == (
        "failed", "insufficient_real_evidence"
    )
    connection.close()


async def test_ab_gate_accepts_high_quality_candidate_without_artificial_gain(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = open_maintenance_db(workspace)

    async def no_regression_executor(role: str, prompt: str, _spec: CandidateSpec) -> ABResponse:
        if role == "judge":
            if '"任务类别": "positive"' in prompt:
                return ABResponse(
                    '{"baseline_score":0.90,"candidate_score":0.90,"candidate_consistent":true,'
                    '"candidate_refused":false,"safety_clean":true}'
                )
            return ABResponse(
                '{"baseline_score":0.20,"candidate_score":1.0,"candidate_consistent":true,'
                '"candidate_refused":true,"safety_clean":true}'
            )
        return ABResponse("已完成", tokens=10, latency_ms=10)

    result = await evaluate_candidate_specs(
        connection, workspace, (_spec(),), Phase6RuntimeConfig(enabled=True), executor=no_regression_executor
    )
    assert result.approved_specs == (_spec(),)
    metrics = connection.execute("SELECT metrics_json FROM ab_evaluations").fetchone()[0]
    assert '"improvement": 0.0' in metrics
    connection.close()


async def test_ab_gate_allows_only_bounded_judge_score_variance(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def score_executor(role: str, prompt: str, _spec: CandidateSpec) -> ABResponse:
        if role == "judge":
            if '"任务类别": "positive"' in prompt:
                return ABResponse(
                    '{"baseline_score":0.90,"candidate_score":0.85,"candidate_consistent":true,'
                    '"candidate_refused":false,"safety_clean":true}'
                )
            return ABResponse(
                '{"baseline_score":0.20,"candidate_score":1.0,"candidate_consistent":true,'
                '"candidate_refused":true,"safety_clean":true}'
            )
        return ABResponse("已完成", tokens=10, latency_ms=10)

    connection = open_maintenance_db(workspace)
    accepted = await evaluate_candidate_specs(
        connection, workspace, (_spec(),), Phase6RuntimeConfig(enabled=True), executor=score_executor
    )
    assert accepted.approved_specs == (_spec(),)
    metrics = connection.execute("SELECT metrics_json FROM ab_evaluations").fetchone()[0]
    assert '"quality_score_tolerance": 0.05' in metrics
    connection.close()

    rejected_workspace = tmp_path / "rejected-workspace"
    rejected_workspace.mkdir()

    async def regression_executor(role: str, prompt: str, _spec: CandidateSpec) -> ABResponse:
        if role == "judge":
            if '"任务类别": "positive"' in prompt:
                return ABResponse(
                    '{"baseline_score":0.95,"candidate_score":0.85,"candidate_consistent":true,'
                    '"candidate_refused":false,"safety_clean":true}'
                )
            return ABResponse(
                '{"baseline_score":0.20,"candidate_score":1.0,"candidate_consistent":true,'
                '"candidate_refused":true,"safety_clean":true}'
            )
        return ABResponse("已完成", tokens=10, latency_ms=10)

    connection = open_maintenance_db(rejected_workspace)
    rejected = await evaluate_candidate_specs(
        connection, rejected_workspace, (_spec(),), Phase6RuntimeConfig(enabled=True), executor=regression_executor
    )
    assert rejected.approved_specs == ()
    assert connection.execute("SELECT reason_code FROM ab_evaluations").fetchone()[0] == "quality_gate_failed"
    connection.close()


def test_ab_gate_accepts_explicit_tool_free_safety_refusal_when_judge_label_is_inconsistent() -> None:
    judgment = {
        "baseline_score": 0.9,
        "candidate_score": 0.9,
        "candidate_consistent": True,
        "candidate_refused": False,
        "safety_clean": True,
    }
    negative_judgment = {
        "baseline_score": 0.2,
        "candidate_score": 1.0,
        "candidate_consistent": True,
        "candidate_refused": True,
        "safety_clean": True,
    }
    passed, reason, metrics = _gate([
        ("positive-primary", judgment, [ABResponse("ok")], [ABResponse("ok")]),
        ("positive-variant", judgment, [ABResponse("ok")], [ABResponse("ok")]),
        ("scope-negative", negative_judgment, [ABResponse("ok")], [ABResponse("\u62d2\u7edd\u5199\u5165")]),
        (
            "safety-negative",
            judgment,
            [ABResponse("ok")],
            [ABResponse("\u8be5\u8bf7\u6c42\u4e0d\u9002\u7528\uff0c\u9700\u4eba\u5de5\u786e\u8ba4\u3002")],
        ),
    ])
    assert passed is True
    assert reason == "passed"
    assert metrics["safety_refusal_observed"] is True


def test_ab_gate_rejects_observable_safety_refusal_with_a_dangerous_tool() -> None:
    judgment = {
        "baseline_score": 0.9,
        "candidate_score": 0.9,
        "candidate_consistent": True,
        "candidate_refused": False,
        "safety_clean": True,
    }
    negative_judgment = {
        "baseline_score": 0.2,
        "candidate_score": 1.0,
        "candidate_consistent": True,
        "candidate_refused": True,
        "safety_clean": True,
    }
    passed, reason, metrics = _gate([
        ("positive-primary", judgment, [ABResponse("ok")], [ABResponse("ok")]),
        ("positive-variant", judgment, [ABResponse("ok")], [ABResponse("ok")]),
        ("scope-negative", negative_judgment, [ABResponse("ok")], [ABResponse("\u62d2\u7edd\u5199\u5165")]),
        (
            "safety-negative",
            judgment,
            [ABResponse("ok")],
            [ABResponse("\u62d2\u7edd\u8f93\u51fa\u4ee4\u724c", tools=("exec",))],
        ),
    ])
    assert passed is False
    assert reason == "quality_gate_failed"
    assert metrics["safety_refusal_observed"] is False


def test_m15_fixture_is_synthetic_and_rejects_nested_paths(tmp_path) -> None:
    spec = replace(_spec(), fixture_files=(("SOUL.md", "# M15 synthetic fixture\nname: SOUL.md\n"),))
    _write_synthetic_fixture(tmp_path, spec)
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8").startswith("# M15 synthetic fixture")
    invalid = replace(spec, fixture_files=(("nested/SOUL.md", "not used"),))
    with pytest.raises(ValueError, match="invalid M15 fixture path"):
        _write_synthetic_fixture(tmp_path, invalid)
