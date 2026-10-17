"""Q3 isolation, bounded failure recovery and atomic Proposal ownership."""

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import nanobot.memory.ab_evaluation as evaluation
from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.memory.evaluation_budget import EvaluationBudget, admit_evaluation
from nanobot.memory.evaluation_provider import EvaluationExecutionError, EvaluationProvider
from nanobot.memory.evolution_orchestrator import run_fixture_review_cycle
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.proposal_repository import ProposalRepository
from nanobot.memory.queue_state import (
    StaleEvaluationLeaseError,
    claim_candidate,
    fail_timed_out_evaluation,
    reap_stale_evaluations,
)
from nanobot.providers.base import LLMProvider, LLMResponse
from tests.memory.test_ab_evaluation import _executor, _spec
from tests.memory.test_evolution_orchestrator import _setup
from tests.memory.test_evolution_orchestrator import _spec as _fixture_spec
from tests.memory.test_queue_state import _seed


def _queued(connection, workspace, name, *, spec=None):
    spec = spec or replace(_spec(), candidate_hash=name)
    _seed(connection, name, digest=spec.candidate_hash)
    connection.execute(
        "UPDATE evolution_task_evidence SET workspace=? WHERE task_id=?",
        (str(workspace), "task:" + name),
    )
    connection.execute(
        "UPDATE evolution_candidate_staging SET workspace=?,candidate_revision_id=? WHERE candidate_id=?",
        (str(workspace), spec.candidate_revision_id, name),
    )
    connection.commit()
    return replace(spec, origin_candidate_id=name)


async def test_b1_bad_candidate_does_not_stop_other_concurrent_candidates(tmp_path):
    connection = open_maintenance_db(tmp_path)
    specs = tuple(_queued(connection, tmp_path, name) for name in ("bad", "good", "rejected"))
    started = asyncio.Event()

    async def execute(role, prompt, spec):
        if spec.origin_candidate_id == "bad":
            await started.wait()  # A serial evaluator would deadlock here.
            raise ValueError("invalid candidate fixture")
        started.set()
        response = await _executor(role, prompt, spec)
        if spec.origin_candidate_id == "rejected" and role == "judge":
            return replace(response, content=response.content.replace('0.90', '0.10'))
        return response

    result = await asyncio.wait_for(evaluation.evaluate_candidate_specs(
        connection, tmp_path, specs,
        Phase6RuntimeConfig(enabled=True, ab_evaluation_max_candidates_per_day=3,
                            ab_evaluation_max_model_calls_per_day=120), executor=execute,
    ), 2)
    assert result.approved_specs == (specs[1],)
    assert result.model_calls_used == 81 and result.evaluated_count == 3
    assert [tuple(row) for row in connection.execute(
        "SELECT candidate_id,status FROM evolution_candidate_staging ORDER BY candidate_id",
    )] == [("bad", "failed"), ("good", "evaluating"), ("rejected", "failed")]
    assert connection.execute("SELECT status FROM ab_evaluations WHERE candidate_hash='bad'").fetchone()[0] == "failed"
    assert connection.execute("SELECT count(*) FROM skills").fetchone()[0] == 0


async def test_b2_whole_candidate_timeout_is_final_and_keeps_dispatched_charge(tmp_path):
    connection = open_maintenance_db(tmp_path)
    spec = _queued(connection, tmp_path, "slow")
    config = Phase6RuntimeConfig(enabled=True, evaluation_timeout_seconds=1)
    dispatched = 0

    async def execute(role, prompt, spec):
        nonlocal dispatched
        dispatched += 1
        await asyncio.Event().wait()

    first = await evaluation.evaluate_candidate_specs(connection, tmp_path, (spec,), config, executor=execute)
    second = await evaluation.evaluate_candidate_specs(connection, tmp_path, (spec,), config, executor=execute)
    assert not first.approved_specs and first.model_calls_used == 1 and second.model_calls_used == 0
    assert dispatched == 1
    assert tuple(connection.execute(
        "SELECT status,last_error,attempt_count,lease_owner FROM evolution_candidate_staging",
    ).fetchone()) == ("failed", "evaluation_timeout", 1, None)
    assert tuple(connection.execute("SELECT status,reason_code FROM ab_evaluations").fetchone()) == (
        "failed", "evaluation_timeout",
    )
    assert tuple(connection.execute(
        "SELECT actual_calls,reserved_calls,unconfirmed_calls FROM evaluation_model_call_reservations",
    ).fetchone()) == (1, 0, 0)
    assert connection.execute("SELECT settled FROM evaluation_candidate_admissions").fetchone()[0] == 1


@pytest.mark.parametrize("budget_limit,max_attempts,expected", [(40, 3, "failed"), (80, 1, "failed"), (80, 3, "queued")])
async def test_recoverable_failure_requires_both_attempts_and_remaining_real_budget(tmp_path, budget_limit, max_attempts, expected):
    connection = open_maintenance_db(tmp_path)
    spec = _queued(connection, tmp_path, "retry")
    config = Phase6RuntimeConfig(enabled=True, ab_evaluation_max_model_calls_per_day=budget_limit,
                                 evaluation_max_attempts=max_attempts)

    async def unavailable(*args):
        raise ConnectionError("provider transport unavailable")

    await evaluation.evaluate_candidate_specs(connection, tmp_path, (spec,), config, executor=unavailable)
    assert tuple(connection.execute(
        "SELECT status,attempt_count FROM evolution_candidate_staging",
    ).fetchone()) == (expected, 1)
    retried = await evaluation.evaluate_candidate_specs(connection, tmp_path, (spec,), config, executor=_executor)
    assert bool(retried.approved_specs) == (expected == "queued")
    assert connection.execute("SELECT sum(actual_calls) FROM evaluation_model_call_reservations").fetchone()[0] == (
        41 if expected == "queued" else 1
    )


async def test_recoverable_failure_converges_at_attempt_limit_without_resetting_budget(tmp_path):
    connection = open_maintenance_db(tmp_path)
    spec = _queued(connection, tmp_path, "retry")
    config = Phase6RuntimeConfig(enabled=True, ab_evaluation_max_model_calls_per_day=120)

    async def unavailable(*args):
        raise TimeoutError("one provider request timed out")

    for _ in range(5):
        await evaluation.evaluate_candidate_specs(connection, tmp_path, (spec,), config, executor=unavailable)
    assert tuple(connection.execute(
        "SELECT status,last_error,attempt_count FROM evolution_candidate_staging",
    ).fetchone()) == ("failed", "evaluation_error", 3)
    assert connection.execute("SELECT count(*) FROM ab_evaluations").fetchone()[0] == 3
    assert connection.execute("SELECT sum(actual_calls) FROM evaluation_model_call_reservations").fetchone()[0] == 3


async def test_cancelled_parent_remains_cancelled_and_does_not_fabricate_timeout(tmp_path):
    connection = open_maintenance_db(tmp_path)
    spec = _queued(connection, tmp_path, "cancel")
    entered = asyncio.Event()

    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(evaluation.evaluate_candidate_specs(
        connection, tmp_path, (spec,), Phase6RuntimeConfig(enabled=True), executor=wait,
    ))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tuple(connection.execute("SELECT status,reason_code FROM ab_evaluations").fetchone()) == (
        "error", "evaluation_cancelled",
    )
    assert connection.execute("SELECT status FROM evolution_candidate_staging").fetchone()[0] == "evaluating"
    assert tuple(connection.execute(
        "SELECT actual_calls,reserved_calls FROM evaluation_model_call_reservations",
    ).fetchone()) == (1, 0)


async def test_missing_persisted_sample_round_cannot_pass(tmp_path, monkeypatch):
    connection = open_maintenance_db(tmp_path)
    original = evaluation._sample_round

    async def lose_second_round(*args, **kwargs):
        result = await original(*args, **kwargs)
        connection.execute("DELETE FROM ab_case_results WHERE sample_round=2")
        connection.commit()
        return result

    monkeypatch.setattr(evaluation, "_sample_round", lose_second_round)
    result = await evaluation.evaluate_candidate_specs(
        connection, tmp_path, (_spec(),), Phase6RuntimeConfig(enabled=True), executor=_executor,
    )
    assert not result.approved_specs and result.model_calls_used == 40
    assert connection.execute("SELECT reason_code FROM ab_evaluations").fetchone()[0] == "evaluation_error"


class _UnavailableProvider(LLMProvider):
    _CHAT_RETRY_DELAYS = ()

    def get_default_model(self):
        return "unavailable-test-model"

    async def chat(self, **kwargs):
        return LLMResponse(content="unavailable", finish_reason="error", error_kind="connection")


async def test_real_provider_error_response_is_not_scored_as_candidate_answer(tmp_path):
    connection = open_maintenance_db(tmp_path)
    config = Phase6RuntimeConfig(enabled=True)
    admission = admit_evaluation(connection, str(tmp_path), _spec(), config)
    budget = EvaluationBudget(connection, str(tmp_path), admission.evaluation_id, config)
    provider = EvaluationProvider(_UnavailableProvider(), budget)
    with pytest.raises(EvaluationExecutionError) as exc:
        await provider.chat_with_retry(messages=[{"role": "user", "content": "fixture"}])
    assert exc.value.retryable and provider.execution_error is exc.value
    assert budget.used_calls() == 1


def _proposal(repo, workspace, spec, lease=None, **extra):
    return repo.create_proposal(
        workspace=str(workspace), skill_name=spec.skill_name, source_kind=spec.source_kind,
        target="workspace_adopt_proposal", baseline_hash=spec.baseline_hash,
        candidate_hash=spec.candidate_hash, baseline_revision_id=spec.baseline_revision_id,
        candidate_revision_id=spec.candidate_revision_id, evaluation_lease=lease, **extra,
    )


def test_proposal_consumes_lease_and_insert_in_one_transaction_with_rollback(tmp_path):
    workspace, connection = _setup(tmp_path)
    spec = _queued(connection, workspace, "atomic", spec=_fixture_spec())
    lease = claim_candidate(connection, workspace=str(workspace), owner="worker")
    repo = ProposalRepository(connection)
    connection.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON skill_proposals BEGIN SELECT RAISE(ABORT,'fixture insert failure'); END")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(repo, workspace, spec, lease)
    assert tuple(connection.execute(
        "SELECT status,lease_owner FROM evolution_candidate_staging",
    ).fetchone()) == ("evaluating", "worker")
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0
    connection.execute("DROP TRIGGER fail_insert")
    connection.commit()
    _proposal(repo, workspace, spec, lease)
    assert tuple(connection.execute(
        "SELECT status,lease_owner,lease_expires_at FROM evolution_candidate_staging",
    ).fetchone()) == ("proposal_eligible", None, None)
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 1
    with pytest.raises(StaleEvaluationLeaseError):
        _proposal(repo, workspace, spec, lease)


def test_old_owner_and_missing_lease_cannot_publish_or_end_replacement(tmp_path):
    workspace, connection = _setup(tmp_path)
    spec = _queued(connection, workspace, "late", spec=_fixture_spec())
    now = datetime.now(UTC)
    old = claim_candidate(connection, workspace=str(workspace), owner="old", now=now-timedelta(seconds=3), lease_seconds=1)
    reap_stale_evaluations(connection, workspace=str(workspace), now=now)
    new = claim_candidate(connection, workspace=str(workspace), owner="new", now=now)
    repo = ProposalRepository(connection)
    for lease in (None, old, replace(new, workspace="other"), replace(new, attempt=1)):
        with pytest.raises(StaleEvaluationLeaseError):
            _proposal(repo, workspace, spec, lease)
    for altered in (replace(spec, candidate_hash="incorrect"), replace(spec, candidate_revision_id="incorrect")):
        with pytest.raises(StaleEvaluationLeaseError):
            _proposal(repo, workspace, altered)
    assert not fail_timed_out_evaluation(connection, old)
    assert tuple(connection.execute(
        "SELECT status,lease_owner,attempt_count FROM evolution_candidate_staging",
    ).fetchone()) == ("evaluating", "new", 2)
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0
    _proposal(repo, workspace, spec, new)


def test_timeout_can_end_same_owner_at_deadline_but_not_another_owner(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _queued(connection, tmp_path, "slow")
    old = claim_candidate(connection, workspace=str(tmp_path), owner="old",
                          now=datetime.now(UTC)-timedelta(seconds=3), lease_seconds=1)
    assert fail_timed_out_evaluation(connection, old)
    assert not fail_timed_out_evaluation(connection, old)
    assert connection.execute("SELECT last_error FROM evolution_candidate_staging").fetchone()[0] == "evaluation_timeout"


def test_fixture_cycle_propagates_valid_lease_and_rejects_stale_without_aborting_batch(tmp_path):
    workspace, connection = _setup(tmp_path)
    spec = _queued(connection, workspace, "cycle", spec=_fixture_spec())
    now = datetime.now(UTC)
    old = claim_candidate(connection, workspace=str(workspace), owner="old", now=now-timedelta(seconds=3), lease_seconds=1)
    reap_stale_evaluations(connection, workspace=str(workspace))
    new = claim_candidate(connection, workspace=str(workspace), owner="new")
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES('other-rev','skill-1','3','other-content','new','test','now')"
    )
    connection.commit()
    other = replace(_fixture_spec(), candidate_hash="other-content", candidate_revision_id="other-rev")
    records = [{"trace_id": f"trace-{i}", "summary": spec.task_key, "outcome": "success"} for i in range(3)]
    result = run_fixture_review_cycle(
        connection, workspace, Phase6RuntimeConfig(enabled=True), trace_records=records,
        candidate_specs=(spec, other), evaluation_leases=(old,),
    )
    assert len(result.proposal_ids) == 1 and result.rejected_task_keys == (spec.task_key,)
    assert connection.execute("SELECT candidate_hash FROM skill_proposals").fetchone()[0] == "other-content"
    valid = run_fixture_review_cycle(
        connection, workspace, Phase6RuntimeConfig(enabled=True), trace_records=records,
        candidate_specs=(spec,), evaluation_leases=(new,),
    )
    assert len(valid.proposal_ids) == 1
    assert connection.execute("SELECT status FROM evolution_candidate_staging").fetchone()[0] == "proposal_eligible"


async def test_superseded_evaluator_cannot_cache_a_final_verdict_or_offer_a_proposal(tmp_path):
    connection = open_maintenance_db(tmp_path)
    spec = _queued(connection, tmp_path, "superseded")
    replaced = False

    async def execute(role, prompt, candidate):
        nonlocal replaced
        if not replaced:
            replaced = True
            connection.execute("UPDATE evolution_candidate_staging SET lease_expires_at='2000-01-01'")
            connection.commit()
            reap_stale_evaluations(connection, workspace=str(tmp_path))
            assert claim_candidate(connection, workspace=str(tmp_path), owner="replacement")
        return await _executor(role, prompt, candidate)

    result = await evaluation.evaluate_candidate_specs(
        connection, tmp_path, (spec,), Phase6RuntimeConfig(enabled=True), executor=execute,
    )
    assert not result.approved_specs and result.model_calls_used == 40
    assert tuple(connection.execute("SELECT status,reason_code FROM ab_evaluations").fetchone()) == (
        "error", "lease_expired",
    )
    assert tuple(connection.execute(
        "SELECT status,lease_owner,attempt_count FROM evolution_candidate_staging",
    ).fetchone()) == ("evaluating", "replacement", 2)
