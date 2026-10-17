"""Optional diagnostics preserve the existing scoring and confidentiality boundaries."""

import json
from dataclasses import replace

import pytest

from nanobot.memory.ab_evaluation import _parse_judgment, evaluate_candidate_specs
from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.migrations.runner import apply_migrations
from tests.memory.test_ab_evaluation import _executor, _spec


@pytest.mark.parametrize("feedback", [None, "", 42, ["invalid"], {"invalid": True}])
async def test_missing_or_malformed_feedback_does_not_fail_scores(tmp_path, feedback):
    async def execute(role, prompt, spec):
        response = await _executor(role, prompt, spec)
        if role == "judge":
            judgment = json.loads(response.content)
            if feedback is not None:
                judgment["feedback"] = feedback
            response = replace(response, content=json.dumps(judgment))
        return response

    connection = open_maintenance_db(tmp_path)
    result = await evaluate_candidate_specs(
        connection, tmp_path, (_spec(),), Phase6RuntimeConfig(enabled=True), executor=execute,
    )
    assert result.approved_specs == (_spec(),)
    assert connection.execute("SELECT status FROM ab_evaluations").fetchone()[0] == "passed"
    assert {row[0] for row in connection.execute("SELECT judge_feedback FROM ab_case_results")} == {""}
    connection.close()


async def test_diagnostics_are_requested_redacted_bounded_and_persisted(tmp_path):
    async def execute(role, prompt, spec):
        response = await _executor(role, prompt, spec)
        if role == "judge":
            assert "可执行诊断" in prompt
            judgment = json.loads(response.content)
            judgment["feedback"] = (
                "缺少结果校验；apiKey=fixture-private-value；"
                "Bearer fixture-private-bearer；`python -c fixture-command`；"
                "成员=0123456789abcdef0123456789abcdef；" + "补充边界检查。" * 100
            )
            response = replace(response, content=json.dumps(judgment))
        return response

    connection = open_maintenance_db(tmp_path)
    result = await evaluate_candidate_specs(
        connection, tmp_path, (_spec(),), Phase6RuntimeConfig(enabled=True), executor=execute,
    )
    assert result.approved_specs == (_spec(),)
    feedback = [row[0] for row in connection.execute("SELECT judge_feedback FROM ab_case_results")]
    assert feedback and all(text.startswith("缺少结果校验") and len(text) <= 500 for text in feedback)
    assert "fixture-private" not in str(feedback)
    assert "fixture-command" not in str(feedback)
    assert "0123456789abcdef" not in str(feedback)
    connection.close()


def test_copied_original_content_is_not_kept_in_diagnostics():
    original = "original evaluation response that must not enter diagnostics"
    judgment = json.dumps({
        "baseline_score": 0.7, "candidate_score": 0.9,
        "candidate_consistent": True, "candidate_refused": False, "safety_clean": True,
        "feedback": "缺少校验：" + original,
    })
    parsed = _parse_judgment(judgment, original_texts=(original,))
    assert parsed is not None
    assert original not in parsed["feedback"]
    assert parsed["candidate_score"] == 0.9


def test_feedback_upgrade_preserves_old_case_and_defaults_to_empty(tmp_path):
    connection = open_maintenance_db(tmp_path)
    connection.execute("ALTER TABLE ab_case_results DROP COLUMN judge_feedback")
    connection.execute("DELETE FROM schema_meta WHERE version=22")
    connection.execute(
        "INSERT INTO ab_evaluations(evaluation_id,workspace,skill_name,candidate_hash,mode,model_id,"
        "reasoning_effort,real_evidence_count,max_model_calls,status,reason_code,created_at) "
        "VALUES('old-eval','workspace','skill','hash','enforced','model','high',3,40,'passed','passed','before')",
    )
    connection.execute(
        "INSERT INTO ab_case_results(result_id,evaluation_id,case_key,split,attempt,baseline_digest,"
        "candidate_digest,safety_clean,judge_status,created_at) "
        "VALUES('old-case','old-eval','case','positive',1,'base','candidate',1,'passed','before')",
    )
    connection.commit()
    apply_migrations(connection)
    assert tuple(connection.execute(
        "SELECT result_id,judge_status,created_at,judge_feedback FROM ab_case_results",
    ).fetchone()) == ("old-case", "passed", "before", "")
    apply_migrations(connection)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
