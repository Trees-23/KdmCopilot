"""Per-event observed counts survive callbacks, corrections and transaction failure."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from nanobot.memory.asset_retriever import retrieve_asset_context
from nanobot.memory.asset_signals import account_asset_injection, settle_asset_injections
from nanobot.memory.db import connect_memory_db
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.retrieval_events import RetrievalEvent, record_retrieval
from tests.memory.test_asset_retrieval import _asset


def _counts(connection, name="known"):
    return tuple(connection.execute(
        "SELECT injection_count,post_injection_success_count,post_injection_failure_count "
        "FROM memory_assets WHERE asset_id=?", (name,),
    ).fetchone())


def _inject(workspace, trace="trace", turn="turn"):
    return retrieve_asset_context("fixture", workspace=workspace, session_key="session",
                                  trace_id=trace, turn_id=turn)


@pytest.mark.parametrize("outcome,expected", [
    ("success", (1, 1, 0)), ("recovered", (1, 1, 0)), ("failure", (1, 0, 1)),
    ("partial", (1, 0, 0)), (None, (1, 0, 0)),
])
def test_actual_injection_and_four_outcomes_are_separate_and_idempotent(tmp_path, outcome, expected):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    result = _inject(tmp_path)
    assert result.outcome == "indexed" and result.injected_ids == ("known",)
    account_asset_injection(connection, result.retrieval_id, workspace=str(tmp_path))
    for _ in range(3):
        settle_asset_injections(connection, workspace=str(tmp_path), trace_id="trace", outcome=outcome)
    assert _counts(connection) == expected
    assert connection.execute("SELECT last_injected_at FROM memory_assets").fetchone()[0]
    assert connection.execute("SELECT asset_settled_outcome FROM retrieval_events").fetchone()[0] == outcome


def test_multiple_turns_and_multiple_events_in_one_trace_count_independently(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    for trace, turn in (("first", "1"), ("second", "2"), ("second", "3")):
        assert _inject(tmp_path, trace, turn).injected_ids == ("known",)
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="first", outcome="failure")
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="second", outcome="success")
    assert _counts(connection) == (3, 2, 1)
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="first", outcome="recovered")
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="first", outcome="failure")
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="first", outcome="recovered")
    assert _counts(connection) == (3, 3, 0)
    assert connection.execute("SELECT count(*) FROM retrieval_events").fetchone()[0] == 3


def test_partial_can_be_corrected_without_fabricating_a_failed_result(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    _inject(tmp_path)
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="trace", outcome="partial")
    assert _counts(connection) == (1, 0, 0)
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="trace", outcome="recovered")
    assert _counts(connection) == (1, 1, 0)
    with pytest.raises(ValueError):
        settle_asset_injections(connection, workspace=str(tmp_path), trace_id="trace", outcome="completed")


def test_only_budgeted_assets_count_and_no_other_retrieval_scopes_are_settled(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    _asset(connection, tmp_path, "long", text="fixture " + "long " * 2000)
    result = retrieve_asset_context("fixture", workspace=tmp_path, session_key="s", trace_id="trace", turn_id="t", token_budget=180)
    assert result.injected_ids == ("known",)
    record_retrieval(connection, RetrievalEvent(
        session_key="s", intent="task", scopes=["skill"], result_ids=["known"],
        injected_ids=["known"], injected_context=["fixture"], outcome="indexed", trace_id="trace",
    ))
    connection.commit()
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="trace", outcome="success")
    assert _counts(connection) == (1, 1, 0) and _counts(connection, "long") == (0, 0, 0)
    assert _inject(tmp_path, "empty", "t").outcome == "indexed"
    empty = retrieve_asset_context("unrelated-ocean-geology", workspace=tmp_path, session_key="s", trace_id="empty-query", turn_id="t")
    assert empty.outcome == "empty"
    assert connection.execute("SELECT asset_workspace FROM retrieval_events WHERE retrieval_id=?", (empty.retrieval_id,)).fetchone()[0] is None


def test_workspace_validation_rolls_back_all_assets_and_does_not_leak_settlement(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    _asset(connection, tmp_path / "other", "foreign")
    event_id = record_retrieval(connection, RetrievalEvent(
        session_key="s", intent="task", scopes=["asset"], result_ids=["known", "foreign"],
        injected_ids=["known", "foreign"], injected_context=["fixture"], outcome="indexed", trace_id="trace",
    ))
    connection.commit()
    with pytest.raises(ValueError):
        account_asset_injection(connection, event_id, workspace=str(tmp_path))
    assert _counts(connection) == _counts(connection, "foreign") == (0, 0, 0)
    result = _inject(tmp_path)
    assert result.injected_ids == ("known",)
    settle_asset_injections(connection, workspace=str(tmp_path / "other"), trace_id="trace", outcome="success")
    assert _counts(connection) == (1, 0, 0)
    with pytest.raises(ValueError):
        account_asset_injection(connection, result.retrieval_id, workspace=str(tmp_path / "other"))


def test_failed_injection_transaction_leaves_no_event_or_untracked_context(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    connection.execute("CREATE TRIGGER reject_signal BEFORE UPDATE OF injection_count ON memory_assets BEGIN SELECT RAISE(ABORT,'count failed'); END")
    connection.commit()
    result = _inject(tmp_path)
    assert result.outcome == "degraded" and result.error_code == "asset_event_write_failed"
    assert not result.text and not result.hits
    assert connection.execute("SELECT count(*) FROM retrieval_events").fetchone()[0] == 0
    assert _counts(connection) == (0, 0, 0)


def test_failed_settlement_rolls_back_entire_trace_and_is_retryable(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    _asset(connection, tmp_path, "second")
    assert len(_inject(tmp_path).injected_ids) == 2
    connection.execute("CREATE TRIGGER reject_result BEFORE UPDATE OF post_injection_success_count ON memory_assets WHEN NEW.asset_id='second' BEGIN SELECT RAISE(ABORT,'result failed'); END")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        settle_asset_injections(connection, workspace=str(tmp_path), trace_id="trace", outcome="success")
    assert _counts(connection) == _counts(connection, "second") == (1, 0, 0)
    assert connection.execute("SELECT asset_settled_outcome FROM retrieval_events").fetchone()[0] is None
    connection.execute("DROP TRIGGER reject_result")
    connection.commit()
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="trace", outcome="success")
    assert _counts(connection) == _counts(connection, "second") == (1, 1, 0)


def test_concurrent_callbacks_and_corrections_count_each_event_once(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    _inject(tmp_path)

    def callback(outcome):
        conn = connect_memory_db(tmp_path)
        try:
            settle_asset_injections(conn, workspace=str(tmp_path), trace_id="trace", outcome=outcome)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(callback, ["failure", "recovered", "failure", "recovered", "success"] * 4))
    assert _counts(connection) == (1, 1, 0)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_preserves_old_events_without_backfilling_observed_counts(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    event_id = record_retrieval(connection, RetrievalEvent(
        session_key="old", intent="task", scopes=["asset"], result_ids=["known"],
        injected_ids=["known"], injected_context=["old"], outcome="indexed", trace_id="old-trace",
    ))
    connection.execute("DROP INDEX retrieval_asset_settlement")
    connection.execute("ALTER TABLE retrieval_events DROP COLUMN asset_settled_outcome")
    connection.execute("ALTER TABLE retrieval_events DROP COLUMN asset_workspace")
    connection.execute("DELETE FROM schema_meta WHERE version=30")
    connection.commit()
    original = dict(connection.execute("SELECT * FROM retrieval_events").fetchone())
    apply_migrations(connection)
    apply_migrations(connection)
    row = dict(connection.execute("SELECT * FROM retrieval_events WHERE retrieval_id=?", (event_id,)).fetchone())
    assert {key: row[key] for key in original} == original
    assert row["asset_workspace"] is None and row["asset_settled_outcome"] is None
    settle_asset_injections(connection, workspace=str(tmp_path), trace_id="old-trace", outcome="success")
    assert _counts(connection) == (0, 0, 0)
    assert json.loads(row["injected_ids_json"]) == ["known"]
