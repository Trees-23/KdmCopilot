"""Q5 durable trigger identity, leases, fencing and bounded retry behavior."""

from datetime import UTC, datetime, timedelta

import pytest

from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.work_queue import (
    MAX_ATTEMPTS,
    claim_work,
    enqueue_work,
    finish_work,
    reap_work,
    renew_work,
)

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def test_trigger_is_idempotent_even_after_terminal_completion(tmp_path):
    connection = open_maintenance_db(tmp_path)
    first = enqueue_work(connection, str(tmp_path), "review", "phase6:20261016", now=NOW)
    assert enqueue_work(connection, str(tmp_path), "review", "phase6:20261016", now=NOW) == first
    lease = claim_work(connection, str(tmp_path), "worker-a", now=NOW)
    assert lease and finish_work(connection, lease, now=NOW + timedelta(seconds=1))
    assert enqueue_work(connection, str(tmp_path), "review", "phase6:20261016", now=NOW + timedelta(days=1)) == first
    assert connection.execute("SELECT count(*) FROM evolution_work_items").fetchone()[0] == 1


def test_concurrent_kinds_are_independent_and_claimed_fifo(tmp_path):
    connection = open_maintenance_db(tmp_path)
    review = enqueue_work(connection, str(tmp_path), "review", "review:1", now=NOW)
    lesson = enqueue_work(connection, str(tmp_path), "lesson", "lesson:1", now=NOW)
    first = claim_work(connection, str(tmp_path), "worker-a", now=NOW)
    second = claim_work(connection, str(tmp_path), "worker-b", now=NOW)
    assert {first.work_id, second.work_id} == {review, lesson}
    assert first.owner == "worker-a" and second.owner == "worker-b"


def test_expired_lease_requeues_before_max_attempts_and_fences_stale_owner(tmp_path):
    connection = open_maintenance_db(tmp_path)
    enqueue_work(connection, str(tmp_path), "review", "review:1", now=NOW)
    old = claim_work(connection, str(tmp_path), "worker-a", now=NOW)
    assert old
    expired = NOW + timedelta(seconds=121)
    assert reap_work(connection, str(tmp_path), now=expired) == 1
    fresh = claim_work(connection, str(tmp_path), "worker-b", now=expired)
    assert fresh and fresh.generation > old.generation
    assert not renew_work(connection, old, now=expired)
    assert renew_work(connection, fresh, now=expired)
    assert not finish_work(connection, old, now=expired)


def test_failures_requeue_then_terminally_fail_after_bounded_attempts(tmp_path):
    connection = open_maintenance_db(tmp_path)
    enqueue_work(connection, str(tmp_path), "lesson", "lesson:1", now=NOW)
    for attempt in range(MAX_ATTEMPTS):
        lease = claim_work(connection, str(tmp_path), "worker", now=NOW + timedelta(minutes=attempt))
        assert lease
        assert finish_work(connection, lease, error_code="provider_error", now=NOW + timedelta(minutes=attempt))
    assert tuple(connection.execute("SELECT status,attempt_count FROM evolution_work_items").fetchone()) == ("failed", MAX_ATTEMPTS)


def test_worker_build_record_is_atomic_and_mismatch_refuses(tmp_path):
    from nanobot.memory.evolution_worker import WorkerBuildMismatchError, verify_worker_build

    verify_worker_build(tmp_path, "git-a", "git-a")
    assert (tmp_path / "evolution-worker/build.json").is_file()
    with pytest.raises(WorkerBuildMismatchError):
        verify_worker_build(tmp_path, "git-a", "git-b")
    assert '"status": "rejected"' in (tmp_path / "evolution-worker/build.json").read_text()


@pytest.mark.asyncio
async def test_worker_tick_executes_and_acknowledges_claimed_work_without_gateway_loop(tmp_path):
    from types import SimpleNamespace

    from nanobot.memory.evolution_worker import worker_tick

    connection = open_maintenance_db(tmp_path)
    enqueue_work(connection, str(tmp_path), "lesson", "lesson:worker-test", now=NOW)
    connection.close()
    config = SimpleNamespace(
        workspace_path=tmp_path,
        phase6=SimpleNamespace(
            enabled=True,
            kill_switch=False,
            evolution=SimpleNamespace(evaluation_max_attempts=3),
        ),
    )
    seen = []

    async def execute(kind, _config):
        seen.append(kind)
        return {"status": "completed"}

    result = await worker_tick(config, owner="worker-test", execute=execute)
    assert result["status"] == "acknowledged" and seen == ["lesson"]
    connection = open_maintenance_db(tmp_path)
    assert tuple(connection.execute("SELECT status,attempt_count FROM evolution_work_items").fetchone()) == ("completed", 1)


@pytest.mark.asyncio
async def test_worker_tick_records_bounded_exception_and_schedules_retry(tmp_path):
    from types import SimpleNamespace

    from nanobot.memory.evolution_worker import worker_tick

    connection = open_maintenance_db(tmp_path)
    enqueue_work(connection, str(tmp_path), "review", "review:failure", now=NOW)
    connection.close()
    config = SimpleNamespace(workspace_path=tmp_path, phase6=SimpleNamespace(
        enabled=True, kill_switch=False, evolution=SimpleNamespace(evaluation_max_attempts=3),
    ))

    async def fail(_kind, _config):
        raise ValueError("sensitive details are not persisted")

    result = await worker_tick(config, owner="worker-failure", execute=fail)
    assert result["status"] == "acknowledged"
    connection = open_maintenance_db(tmp_path)
    row = connection.execute("SELECT status,attempt_count,error_code FROM evolution_work_items").fetchone()
    assert tuple(row) == ("queued", 1, "ValueError")
    assert "sensitive details" not in str(row)
    assert claim_work(connection, str(tmp_path), "too-early", now=NOW) is None
