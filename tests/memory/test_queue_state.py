"""Real SQLite upgrade, concurrent ownership and crash-recovery checks for Q1."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.memory.db import connect_memory_db_path
from nanobot.memory.migrations import runner
from nanobot.memory.queue_state import (
    claim_candidate,
    finish_evaluation,
    reap_stale_evaluations,
    transition_candidate,
)

NOW = datetime(2026, 10, 16, tzinfo=UTC)
OLD_STATES = {
    "candidate_staged": "queued", "manual_review_required": "manual_review",
    "evaluating": "queued", "rejected_by_quality_gate": "closed",
    "insufficient_evidence": "closed", "proposal_eligible": "proposal_eligible",
    "adopted": "adopted", "closed": "closed",
}


def _db(path=":memory:"):
    connection = connect_memory_db_path(path)
    runner.apply_migrations(connection)
    return connection


def _seed(connection, candidate="candidate", *, status="queued", created="before", digest=None):
    connection.execute(
        "INSERT INTO evolution_task_evidence(task_id,workspace,qualification,redaction_status,"
        "created_at,updated_at) VALUES(?,'workspace','candidate_eligible','safe','before','before')",
        ("task:" + candidate,),
    )
    connection.execute(
        "INSERT INTO evolution_candidate_staging(candidate_id,workspace,task_id,source_kind,"
        "skill_name,candidate_revision_id,candidate_hash,candidate_content,status,reason,created_at,updated_at) "
        "VALUES(?,'workspace',?,'skill_candidate','skill',?,?,?,?,'original reason',?,'original update')",
        (candidate, "task:" + candidate, "revision:" + candidate, digest or "hash:" + candidate,
         "content:" + candidate, status, created),
    )
    connection.commit()


def _legacy_db(*, unique=True):
    connection = _db()
    connection.execute("DROP TABLE evolution_candidate_staging")
    legacy = (Path(runner.__file__).parent / "0015_candidate_adoption_states.sql").read_text()
    ddl = legacy[legacy.index("CREATE TABLE evolution_candidate_staging_v15"):
                 legacy.index("INSERT INTO evolution_candidate_staging_v15")]
    ddl = ddl.replace("evolution_candidate_staging_v15", "evolution_candidate_staging")
    if not unique:
        ddl = ddl.replace(",\n  UNIQUE(workspace,candidate_hash)", "")
    connection.execute(ddl)
    connection.execute("DELETE FROM schema_meta WHERE version=21")
    connection.commit()
    return connection


def test_upgrade_maps_all_old_states_and_preserves_every_other_column():
    connection = _legacy_db()
    for state in OLD_STATES:
        _seed(connection, state, status=state)
    before = [dict(row) for row in connection.execute("SELECT * FROM evolution_candidate_staging")]
    sql = []
    connection.set_trace_callback(sql.append)

    runner.apply_migrations(connection)

    assert not any(statement.lstrip().startswith("UPDATE evolution_candidate_staging SET status") for statement in sql)
    for old in before:
        new = dict(connection.execute(
            "SELECT * FROM evolution_candidate_staging WHERE candidate_id=?", (old["candidate_id"],),
        ).fetchone())
        assert new["status"] == OLD_STATES[old["status"]]
        assert all(new[key] == value for key, value in old.items() if key != "status")
        assert new["attempt_count"] == 0
        assert new["lease_owner"] is None and new["lease_expires_at"] is None
        assert new["last_error"] == ("lease_expired" if old["status"] == "evaluating" else None)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    meta = tuple(connection.execute("SELECT * FROM schema_meta WHERE version=21").fetchone())
    runner.apply_migrations(connection)
    assert tuple(connection.execute("SELECT * FROM schema_meta WHERE version=21").fetchone()) == meta


@pytest.mark.parametrize("bad_data", ["unknown_state", "duplicate_hash"])
def test_upgrade_validates_before_creating_a_new_table(bad_data):
    connection = _legacy_db(unique=bad_data != "duplicate_hash")
    if bad_data == "unknown_state":
        connection.execute("PRAGMA ignore_check_constraints=ON")
        _seed(connection, status="unmapped")
        connection.execute("PRAGMA ignore_check_constraints=OFF")
    else:
        _seed(connection, "a", status="candidate_staged", digest="duplicate")
        _seed(connection, "b", status="candidate_staged", digest="duplicate")
    before = [tuple(row) for row in connection.execute("SELECT * FROM evolution_candidate_staging")]

    with pytest.raises(runner.MigrationError, match="unmapped|duplicate"):
        runner.apply_migrations(connection)

    assert [tuple(row) for row in connection.execute("SELECT * FROM evolution_candidate_staging")] == before
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE name='evolution_candidate_staging_v21'",
    ).fetchone() is None


def test_mid_upgrade_failure_rolls_back_and_next_start_recovers(monkeypatch):
    connection = _legacy_db()
    _seed(connection, status="candidate_staged")
    before = tuple(connection.execute("SELECT * FROM evolution_candidate_staging").fetchone())
    path = runner.QUEUE_LEASES_MIGRATION_PATH
    broken = path.read_text().replace("DROP TABLE evolution_candidate_staging;", "INVALID SQL;")
    monkeypatch.setattr(runner, "QUEUE_LEASES_MIGRATION_PATH", SimpleNamespace(read_text=lambda **_: broken))

    with pytest.raises(runner.MigrationError):
        runner.apply_migrations(connection)
    assert tuple(connection.execute("SELECT * FROM evolution_candidate_staging").fetchone()) == before
    assert connection.execute("SELECT status FROM schema_meta WHERE version=21").fetchone()[0] == "failed"
    monkeypatch.setattr(runner, "QUEUE_LEASES_MIGRATION_PATH", path)
    runner.apply_migrations(connection)
    assert connection.execute("SELECT status FROM evolution_candidate_staging").fetchone()[0] == "queued"


def test_new_database_replays_immutable_history_and_leaves_reserved_versions_empty():
    connection = _db()
    assert {row[0] for row in connection.execute("SELECT version FROM schema_meta")} == {*range(1, 23), 25, 26, 27, 28, 29, 30, 31}
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("SELECT count(*) FROM evolution_candidate_staging").fetchone()[0] == 0


def test_fifo_claim_and_competing_connections_share_one_execution(tmp_path):
    first = _db(tmp_path / "memory.sqlite3")
    second = _db(tmp_path / "memory.sqlite3")
    _seed(first, "newer", created="2026-10-15T02:00:00+00:00")
    _seed(first, "older", created="2026-10-15T01:00:00+00:00")
    lease = claim_candidate(first, workspace="workspace", owner="first", now=NOW)
    assert lease is not None and lease.candidate_id == "older" and lease.attempt == 1
    assert claim_candidate(second, workspace="workspace", owner="second", candidate_id="older", now=NOW) is None
    assert tuple(second.execute(
        "SELECT status,attempt_count,lease_owner,lease_expires_at FROM evolution_candidate_staging WHERE candidate_id='older'",
    ).fetchone()) == ("evaluating", 1, "first", lease.expires_at)
    assert second.execute("SELECT status FROM evolution_candidate_staging WHERE candidate_id='newer'").fetchone()[0] == "queued"


def test_reaper_recovers_crash_and_stale_owner_cannot_overwrite_new_result():
    connection = _db()
    _seed(connection)
    old = claim_candidate(connection, workspace="workspace", owner="worker", now=NOW, lease_seconds=10)
    assert old is not None
    assert not finish_evaluation(connection, old, status="failed", now=NOW + timedelta(seconds=11))
    assert reap_stale_evaluations(connection, workspace="workspace", now=NOW + timedelta(seconds=11)) == 1
    new = claim_candidate(connection, workspace="workspace", owner="worker", now=NOW + timedelta(seconds=12))
    assert new is not None and new.attempt == 2
    assert not finish_evaluation(connection, old, status="failed", now=NOW + timedelta(seconds=13))
    assert finish_evaluation(connection, new, status="proposal_eligible", now=NOW + timedelta(seconds=13))
    assert not finish_evaluation(connection, new, status="failed", now=NOW + timedelta(seconds=14))
    assert tuple(connection.execute(
        "SELECT status,lease_owner,lease_expires_at FROM evolution_candidate_staging",
    ).fetchone()) == ("proposal_eligible", None, None)


def test_reaper_stops_at_attempt_limit_and_failed_candidate_never_requeues():
    connection = _db()
    _seed(connection)
    for attempt in range(3):
        now = NOW + timedelta(seconds=attempt * 20)
        lease = claim_candidate(connection, workspace="workspace", owner="worker", lease_seconds=10, now=now)
        assert lease is not None and lease.attempt == attempt + 1
        assert reap_stale_evaluations(connection, workspace="workspace", now=now + timedelta(seconds=11)) == 1
    assert tuple(connection.execute(
        "SELECT status,attempt_count,last_error,lease_owner,lease_expires_at FROM evolution_candidate_staging",
    ).fetchone()) == ("failed", 3, "lease_expired", None, None)
    assert claim_candidate(connection, workspace="workspace", owner="worker", now=NOW + timedelta(minutes=2)) is None
    assert not transition_candidate(connection, "candidate", status="queued")


def test_execution_cannot_release_its_last_attempt_back_into_queue():
    connection = _db()
    _seed(connection)
    lease = claim_candidate(connection, workspace="workspace", owner="worker", max_attempts=1, now=NOW)
    assert lease is not None
    assert finish_evaluation(connection, lease, status="queued", max_attempts=1, now=NOW)
    assert tuple(connection.execute(
        "SELECT status,last_error FROM evolution_candidate_staging",
    ).fetchone()) == ("failed", "attempts_exhausted")


def test_evaluating_requires_lease_and_reaper_defends_against_invalid_null_lease():
    connection = _db()
    _seed(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE evolution_candidate_staging SET status='evaluating'")
    connection.rollback()
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("UPDATE evolution_candidate_staging SET status='evaluating'")
    connection.commit()
    connection.execute("PRAGMA ignore_check_constraints=OFF")
    assert reap_stale_evaluations(connection, workspace="workspace", now=NOW) == 1
    assert connection.execute("SELECT status FROM evolution_candidate_staging").fetchone()[0] == "queued"


def test_explicit_transition_requires_lease_for_running_and_terminal_gate_results():
    connection = _db()
    _seed(connection, status="manual_review")
    assert transition_candidate(connection, "candidate", status="queued")
    for target in ("evaluating", "failed", "proposal_eligible"):
        with pytest.raises(ValueError, match="lease"):
            transition_candidate(connection, "candidate", status=target)
    assert transition_candidate(connection, "candidate", status="closed")
    assert not transition_candidate(connection, "candidate", status="queued")


@pytest.mark.parametrize(("max_attempts", "expected"), [(3, "queued"), (1, "failed")])
def test_production_scan_reaps_crashed_execution_without_new_candidates(tmp_path, max_attempts, expected):
    from nanobot.config.schema import Phase6Config, Phase6EvolutionConfig
    from nanobot.memory.phase6_trigger import run_phase6_review_scan

    connection = _db(tmp_path / ".nanobot" / "memory.sqlite3")
    _seed(connection)
    workspace = str(tmp_path.resolve())
    connection.execute("UPDATE evolution_task_evidence SET workspace=?", (workspace,))
    connection.execute("UPDATE evolution_candidate_staging SET workspace=?", (workspace,))
    connection.commit()
    lease = claim_candidate(
        connection, workspace=workspace, owner="crashed-worker", lease_seconds=1,
        now=datetime.now(UTC) - timedelta(minutes=2),
    )
    assert lease is not None

    result = run_phase6_review_scan(tmp_path, Phase6Config(
        enabled=True, evolution=Phase6EvolutionConfig(evaluation_max_attempts=max_attempts),
    ))

    assert result.status == "completed"
    assert tuple(connection.execute(
        "SELECT status,last_error,lease_owner,lease_expires_at FROM evolution_candidate_staging",
    ).fetchone()) == (expected, "lease_expired", None, None)


def test_queue_config_aliases_and_runtime_limits():
    from nanobot.config.schema import Phase6Config, Phase6EvolutionConfig
    from nanobot.memory.continuous import Phase6RuntimeConfig

    evolution = Phase6EvolutionConfig.model_validate({
        "evaluationLeaseSeconds": 100, "evaluationTimeoutSeconds": 90, "evaluationMaxAttempts": 2,
    })
    runtime = Phase6RuntimeConfig.from_config(Phase6Config(enabled=True, evolution=evolution))
    assert (runtime.evaluation_lease_seconds, runtime.evaluation_timeout_seconds, runtime.evaluation_max_attempts) == (100, 90, 2)
    with pytest.raises(ValueError):
        Phase6EvolutionConfig(evaluation_max_attempts=4)
    with pytest.raises(ValueError):
        Phase6RuntimeConfig(evaluation_lease_seconds=0)
