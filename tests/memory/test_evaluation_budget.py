"""Content idempotency, two-round convergence and real-request budget enforcement."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

import nanobot.memory.evaluation_budget as accounting
from nanobot.memory.ab_evaluation import evaluate_candidate_specs
from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.memory.db import connect_memory_db_path
from nanobot.memory.evaluation_budget import (
    EvaluationBudget,
    EvaluationBudgetError,
    admit_evaluation,
)
from nanobot.memory.evaluation_provider import EvaluationProvider
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.providers.base import LLMProvider, LLMResponse
from tests.memory.test_ab_evaluation import _executor, _spec
from tests.memory.test_queue_state import _seed


async def test_two_rounds_and_different_feedback_settle_once_and_reuse_by_content(tmp_path):
    calls = 0

    async def execute(role, prompt, spec):
        nonlocal calls
        calls += 1
        response = await _executor(role, prompt, spec)
        if role == "judge":
            value = json.loads(response.content)
            value["feedback"] = "补充边界检查" if calls <= 20 else "增加结果校验"
            response = replace(response, content=json.dumps(value))
        return response

    connection = open_maintenance_db(tmp_path)
    config = Phase6RuntimeConfig(enabled=True)
    first = await evaluate_candidate_specs(connection, tmp_path, (_spec(),), config, executor=execute)
    second = await evaluate_candidate_specs(
        connection, tmp_path, (replace(_spec(), task_key="same-content-different-source"),), config, executor=execute,
    )
    assert first.approved_specs == (_spec(),) and first.model_calls_used == 40
    assert second.approved_specs and second.model_calls_used == 0 and calls == 40
    assert connection.execute("SELECT count(*) FROM ab_evaluations").fetchone()[0] == 1
    assert [tuple(row) for row in connection.execute(
        "SELECT sample_round,count(*) FROM ab_case_results GROUP BY sample_round ORDER BY sample_round",
    )] == [(1, 8), (2, 8)]
    assert {row[0] for row in connection.execute("SELECT judge_feedback FROM ab_case_results")} == {"补充边界检查", "增加结果校验"}
    assert tuple(connection.execute(
        "SELECT settled FROM evaluation_candidate_admissions",
    ).fetchone()) == (1,)
    assert tuple(connection.execute(
        "SELECT actual_calls,reserved_calls FROM evaluation_model_call_reservations",
    ).fetchone()) == (40, 0)


async def test_same_task_key_with_different_hashes_uses_independent_content_slots(tmp_path):
    connection = open_maintenance_db(tmp_path)
    config = Phase6RuntimeConfig(enabled=True, ab_evaluation_max_model_calls_per_day=80)
    specs = (_spec(), replace(_spec(), candidate_hash="second-hash"))
    result = await evaluate_candidate_specs(connection, tmp_path, specs, config, executor=_executor)
    assert result.approved_specs == specs and result.model_calls_used == 80
    assert connection.execute("SELECT count(*) FROM evaluation_candidate_admissions").fetchone()[0] == 2
    denied = await evaluate_candidate_specs(
        connection, tmp_path, (replace(_spec(), candidate_hash="third-hash"),), config, executor=_executor,
    )
    assert denied.status == "budget_exhausted" and denied.model_calls_used == 0
    assert connection.execute("SELECT count(*) FROM ab_evaluations").fetchone()[0] == 2


async def test_legacy_single_round_is_not_reused_but_same_day_calls_still_count(tmp_path):
    connection = open_maintenance_db(tmp_path)
    workspace = str(tmp_path.resolve())
    config = Phase6RuntimeConfig(
        enabled=True, ab_evaluation_max_candidates_per_day=1, ab_evaluation_max_model_calls_per_day=80,
    )
    connection.execute(
        "INSERT INTO ab_evaluations(evaluation_id,workspace,skill_name,candidate_hash,mode,model_id,"
        "reasoning_effort,real_evidence_count,max_model_calls,model_calls_used,status,reason_code,created_at) "
        "VALUES('legacy',?,'skill',?,'enforced','model','high',3,80,20,'passed','passed',?)",
        (workspace, _spec().candidate_hash, accounting._stamp(accounting._now())),
    )
    connection.commit()
    result = await evaluate_candidate_specs(connection, tmp_path, (_spec(),), config, executor=_executor)
    assert result.approved_specs and result.model_calls_used == 40
    assert accounting._usage(connection, workspace, accounting._now()) == (1, 60)


async def test_fifo_queue_does_not_take_lease_when_daily_budget_is_insufficient(tmp_path):
    connection = open_maintenance_db(tmp_path)
    specs = []
    for name, created in (("old", "2026-10-14"), ("new", "2026-10-15")):
        _seed(connection, name, created=created, digest=name)
        specs.append(replace(_spec(), candidate_hash=name, origin_candidate_id=name))
    connection.execute("UPDATE evolution_candidate_staging SET workspace=?", (str(tmp_path.resolve()),))
    connection.execute("UPDATE evolution_task_evidence SET workspace=?", (str(tmp_path.resolve()),))
    connection.commit()

    result = await evaluate_candidate_specs(
        connection, tmp_path, tuple(reversed(specs)), Phase6RuntimeConfig(enabled=True), executor=_executor,
    )
    assert result.approved_specs == (specs[0],) and result.model_calls_used == 40
    assert tuple(connection.execute(
        "SELECT status,attempt_count,lease_owner,lease_expires_at FROM evolution_candidate_staging WHERE candidate_id='new'",
    ).fetchone()) == ("queued", 0, None, None)
    old = connection.execute(
        "SELECT status,attempt_count,lease_expires_at FROM evolution_candidate_staging WHERE candidate_id='old'",
    ).fetchone()
    assert old[0] == "evaluating" and old[1] == 1 and old[2]
    assert len(result.proposal_leases) == 1


async def test_three_uncertain_attempts_charge_all_calls_and_converge_without_proposal(tmp_path, monkeypatch):
    connection = open_maintenance_db(tmp_path)
    config = Phase6RuntimeConfig(enabled=True, ab_evaluation_max_model_calls_per_day=120)
    calls = 0

    async def disagree(role, prompt, spec):
        nonlocal calls
        calls += 1
        response = await _executor(role, prompt, spec)
        if role == "judge" and ((calls - 1) // 20) % 2 == 1 and '"任务类别": "positive"' in prompt:
            value = json.loads(response.content)
            value["candidate_score"] = 0.4
            response = replace(response, content=json.dumps(value))
        return response

    for attempt in range(3):
        # Wall clocks can move backwards (observed under WSL). The attempt order
        # is insertion order, not timestamp order; exercise that case explicitly.
        current_time = datetime(2030, 1, 1, 12, tzinfo=UTC) + timedelta(seconds=(0, -2, -1)[attempt])
        monkeypatch.setattr(accounting, "_now", lambda: current_time)
        result = await evaluate_candidate_specs(connection, tmp_path, (_spec(),), config, executor=disagree)
        assert not result.approved_specs and result.model_calls_used == 40
        assert result.evaluated_count == int(attempt == 2)
        assert connection.execute("SELECT count(*) FROM evaluation_candidate_admissions").fetchone()[0] == 1
        assert connection.execute("SELECT settled FROM evaluation_candidate_admissions").fetchone()[0] == int(attempt == 2)
    assert [tuple(row) for row in connection.execute(
        "SELECT status,reason_code FROM ab_evaluations ORDER BY rowid",
    )] == [("uncertain", "judge_disagreement"), ("uncertain", "judge_disagreement"), ("failed", "judge_unstable")]
    assert calls == 120
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM skills").fetchone()[0] == 0
    cached = await evaluate_candidate_specs(connection, tmp_path, (_spec(),), config, executor=disagree)
    assert not cached.approved_specs and cached.model_calls_used == 0 and calls == 120


def test_simultaneous_content_admission_reserves_one_slot(tmp_path):
    database = tmp_path / ".nanobot" / "memory.sqlite3"
    connection = open_maintenance_db(tmp_path)
    barrier = Barrier(2)
    config = Phase6RuntimeConfig(enabled=True)

    def admit():
        local = connect_memory_db_path(database)
        try:
            barrier.wait()
            return admit_evaluation(local, str(tmp_path), _spec(), config).status
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: admit(), range(2)))
    assert sorted(outcomes) == ["admitted", "running"]
    assert connection.execute("SELECT count(*) FROM evaluation_candidate_admissions").fetchone()[0] == 1
    assert tuple(connection.execute(
        "SELECT reserved_calls,actual_calls FROM evaluation_model_call_reservations",
    ).fetchone()) == (40, 0)


def test_crash_does_not_return_unconfirmed_budget_or_claim_it_as_observed_calls(tmp_path, monkeypatch):
    now = datetime(2026, 10, 16, tzinfo=UTC)
    monkeypatch.setattr(accounting, "_now", lambda: now)
    connection = open_maintenance_db(tmp_path)
    config = Phase6RuntimeConfig(enabled=True, evaluation_timeout_seconds=10)
    admission = admit_evaluation(connection, str(tmp_path), _spec(), config)
    budget = EvaluationBudget(connection, str(tmp_path), admission.evaluation_id, config)
    budget.charge_request()
    now += timedelta(seconds=11)
    denied = admit_evaluation(connection, str(tmp_path), _spec(), config)
    assert denied.status == "budget_exhausted"
    assert tuple(connection.execute(
        "SELECT actual_calls,reserved_calls,unconfirmed_calls,status FROM evaluation_model_call_reservations",
    ).fetchone()) == (1, 0, 39, "abandoned")
    assert budget.used_calls() == 1
    now += timedelta(days=1)
    assert admit_evaluation(connection, str(tmp_path), _spec(), config).status == "admitted"


def test_midnight_keeps_real_calls_on_the_day_they_were_issued(tmp_path, monkeypatch):
    now = datetime(2026, 10, 15, 15, 59, 50, tzinfo=UTC)
    monkeypatch.setattr(accounting, "_now", lambda: now)
    connection = open_maintenance_db(tmp_path)
    config = Phase6RuntimeConfig(enabled=True)
    admission = admit_evaluation(connection, str(tmp_path), _spec(), config)
    budget = EvaluationBudget(connection, str(tmp_path), admission.evaluation_id, config)
    budget.charge_request()
    now += timedelta(seconds=20)
    budget.charge_request()
    budget.settle("error", "fixture-finished")
    assert [tuple(row) for row in connection.execute(
        "SELECT budget_day,actual_calls,reserved_calls FROM evaluation_model_call_reservations ORDER BY budget_day",
    )] == [("2026-10-15", 1, 0), ("2026-10-16", 1, 0)]
    assert budget.used_calls() == 2


class _RetryingProvider(LLMProvider):
    _CHAT_RETRY_DELAYS = (0,)

    def __init__(self):
        super().__init__()
        self.requests = 0

    def get_default_model(self):
        return "budget-fixture-model"

    async def chat(self, **kwargs):
        self.requests += 1
        if self.requests == 1:
            return LLMResponse(content="connection error", finish_reason="error", error_kind="connection")
        return LLMResponse(content="done")


async def test_provider_retries_are_charged_and_budget_guard_stops_before_network(tmp_path):
    connection = open_maintenance_db(tmp_path)
    config = Phase6RuntimeConfig(enabled=True, ab_evaluation_max_model_calls_per_day=1)
    admission = admit_evaluation(connection, str(tmp_path), _spec(), config, required_calls=1)
    budget = EvaluationBudget(connection, str(tmp_path), admission.evaluation_id, config)
    original = _RetryingProvider()
    generation = original.generation
    provider = EvaluationProvider(original, budget)
    with pytest.raises(EvaluationBudgetError):
        await provider.chat_with_retry(messages=[{"role": "user", "content": "fixture"}])
    assert original.requests == 1 and budget.used_calls() == 1
    assert original.generation is generation


async def test_provider_counts_each_real_retry_when_capacity_is_available(tmp_path):
    connection = open_maintenance_db(tmp_path)
    config = Phase6RuntimeConfig(enabled=True)
    admission = admit_evaluation(connection, str(tmp_path), _spec(), config)
    budget = EvaluationBudget(connection, str(tmp_path), admission.evaluation_id, config)
    original = _RetryingProvider()
    response = await EvaluationProvider(original, budget).chat_with_retry(
        messages=[{"role": "user", "content": "fixture"}],
    )
    assert response.content == "done" and original.requests == 2 and budget.used_calls() == 2


def test_budget_migration_preserves_existing_evaluations_cases_and_feedback(tmp_path):
    connection = open_maintenance_db(tmp_path)
    for table in ("evaluation_model_call_reservations", "evaluation_candidate_admissions", "ab_case_results", "ab_evaluations"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("DELETE FROM schema_meta WHERE version=28")
    connection.commit()
    migrations = Path(accounting.__file__).parent / "migrations"
    connection.executescript((migrations / "0008_ab_quality_evaluations.sql").read_text())
    connection.executescript((migrations / "0022_judge_feedback.sql").read_text())
    for index, status in enumerate(("running", "passed", "failed", "budget_exhausted", "error")):
        connection.execute(
            "INSERT INTO ab_evaluations(evaluation_id,workspace,skill_name,candidate_hash,mode,model_id,"
            "reasoning_effort,real_evidence_count,max_model_calls,model_calls_used,status,reason_code,created_at) "
            "VALUES(?,'workspace','skill','same-hash','enforced','model','high',3,40,7,?,'original','before')",
            (f"old-eval-{index}", status),
        )
        connection.execute(
            "INSERT INTO ab_case_results(result_id,evaluation_id,case_key,split,attempt,baseline_digest,"
            "candidate_digest,safety_clean,judge_status,created_at,judge_feedback) "
            "VALUES(?,?,'case','positive',1,'base','candidate',1,'passed','before','历史反馈')",
            (f"old-case-{index}", f"old-eval-{index}"),
        )
    connection.commit()
    old_evaluations = [dict(row) for row in connection.execute("SELECT * FROM ab_evaluations")]
    old_cases = [dict(row) for row in connection.execute("SELECT * FROM ab_case_results")]

    apply_migrations(connection)

    for table, rows, id_column in (("ab_evaluations", old_evaluations, "evaluation_id"), ("ab_case_results", old_cases, "result_id")):
        for row in rows:
            updated = dict(connection.execute(f"SELECT * FROM {table} WHERE {id_column}=?", (row[id_column],)).fetchone())
            assert all(updated[key] == value for key, value in row.items())
    assert {row[0] for row in connection.execute("SELECT protocol_version FROM ab_evaluations")} == {"legacy-v0"}
    assert {row[0] for row in connection.execute("SELECT sample_round FROM ab_case_results")} == {0}
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    apply_migrations(connection)
    assert connection.execute("SELECT count(*) FROM ab_evaluations").fetchone()[0] == 5


def test_reservation_rows_follow_their_source_evaluation_foreign_keys(tmp_path):
    connection = open_maintenance_db(tmp_path)
    admission = admit_evaluation(connection, str(tmp_path), _spec(), Phase6RuntimeConfig(enabled=True))
    connection.execute("DELETE FROM ab_evaluations WHERE evaluation_id=?", (admission.evaluation_id,))
    connection.commit()
    assert connection.execute("SELECT count(*) FROM evaluation_candidate_admissions").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM evaluation_model_call_reservations").fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
