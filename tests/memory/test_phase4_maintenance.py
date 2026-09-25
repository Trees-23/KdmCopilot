from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.memory.maintenance import (
    IDLE_SECONDS,
    claim_due_job,
    complete_job,
    fail_job,
    get_job,
    open_maintenance_db,
    upsert_activity,
)
from nanobot.memory.policy import ToolPolicy
from nanobot.memory.worker import MaintenanceWorker, ReviewResult


def _workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = open_maintenance_db(workspace)
    return workspace, connection


def test_activity_upsert_is_one_job_and_debounces_for_45_minutes(tmp_path):
    workspace, connection = _workspace(tmp_path)
    first = upsert_activity(
        connection, workspace=str(workspace), session_key="websocket:1", message_cursor="m1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = upsert_activity(
        connection, workspace=str(workspace), session_key="websocket:1", message_cursor="m2",
        now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    )
    assert first.job_id == second.job_id
    assert second.activity_epoch == 2
    assert datetime.fromisoformat(second.due_at) == datetime(2026, 1, 1, 0, 1, tzinfo=UTC) + timedelta(seconds=IDLE_SECONDS)
    assert connection.execute("SELECT count(*) FROM maintenance_jobs").fetchone()[0] == 1


def test_worker_lease_recovery_and_stale_epoch_cannot_overwrite_cursor(tmp_path):
    workspace, connection = _workspace(tmp_path)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    job = upsert_activity(connection, workspace=str(workspace), session_key="s", message_cursor="m", now=at)
    claimed = claim_due_job(connection, workspace=str(workspace), worker_id="w1", now=at + timedelta(hours=1))
    assert claimed and claimed.job_id == job.job_id
    assert not complete_job(connection, job_id=job.job_id, worker_id="w1", epoch=999, review_cursor="bad", snapshot_cursor="bad")
    # The lease is recoverable after expiry and the new worker owns the job.
    connection.execute("UPDATE maintenance_jobs SET lease_until=? WHERE job_id=?", ("2025-01-01T00:00:00+00:00", job.job_id))
    connection.commit()
    recovered = claim_due_job(connection, workspace=str(workspace), worker_id="w2", now=at + timedelta(hours=2))
    assert recovered and recovered.worker_id == "w2"
    assert complete_job(connection, job_id=job.job_id, worker_id="w2", epoch=recovered.activity_epoch, review_cursor="r", snapshot_cursor="s")
    assert get_job(connection, job.job_id).last_review_cursor == "r"


def test_failure_backoff_dead_letters_after_five_attempts(tmp_path):
    workspace, connection = _workspace(tmp_path)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    job = upsert_activity(connection, workspace=str(workspace), session_key="s", message_cursor="m", now=at)
    for attempt in range(5):
        claimed = claim_due_job(connection, workspace=str(workspace), worker_id="w", now=at + timedelta(hours=attempt + 1))
        assert claimed
        status = fail_job(connection, job_id=job.job_id, worker_id="w", error="review failed", now=at)
        if attempt < 4:
            connection.execute("UPDATE maintenance_jobs SET due_at=? WHERE job_id=?", (at.isoformat(), job.job_id))
            connection.commit()
    assert status == "dead_letter"


def test_maintenance_worker_runs_review_and_releases_workspace_lock(tmp_path):
    workspace, connection = _workspace(tmp_path)
    upsert_activity(connection, workspace=str(workspace), session_key="s", message_cursor="m", now=datetime(2026, 1, 1, tzinfo=UTC))
    connection.close()
    seen = []
    worker = MaintenanceWorker(workspace, worker_id="worker")
    result = worker.run_once(lambda job: (seen.append(job.job_id) or ReviewResult("r", "s")))
    assert result == "done"
    check = sqlite3.connect(workspace / ".nanobot" / "memory.sqlite3")
    assert check.execute("SELECT status,last_review_cursor FROM maintenance_jobs").fetchone() == ("done", "r")
    assert check.execute("SELECT count(*) FROM maintenance_lock").fetchone()[0] == 0
    check.close()


class _DemoTool(Tool):
    @property
    def name(self): return "write_file"
    @property
    def description(self): return "demo"
    @property
    def parameters(self): return {"type": "object"}
    async def execute(self, **kwargs): return "ok"


def test_tool_policy_blocks_high_risk_calls_before_execution():
    registry = ToolRegistry()
    registry.register(_DemoTool())
    registry.set_tool_policy(ToolPolicy(role="maintenance"))
    ctx = RequestContext(channel="system", chat_id="maintenance", metadata={"agent_role": "maintenance"})
    with request_context(ctx):
        _tool, _params, error = registry.prepare_call("write_file", {})
    assert error is not None
    assert error.error_code == "policy_blocked"
