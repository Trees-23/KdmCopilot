"""Observed reuse counts, with per-injection settlement and no causal claims."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from nanobot.memory.task_outcome import TASK_OUTCOMES


@contextmanager
def _atomic(connection: sqlite3.Connection) -> Iterator[None]:
    # Serialize readers before deriving count deltas. An existing caller's write
    # transaction (the retrieval INSERT) keeps ownership of the final commit.
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    connection.execute("SAVEPOINT asset_signals")
    try:
        yield
    except Exception:
        connection.execute("ROLLBACK TO asset_signals")
        connection.execute("RELEASE asset_signals")
        if owns_transaction:
            connection.rollback()
        raise
    else:
        connection.execute("RELEASE asset_signals")
        if owns_transaction:
            connection.commit()


def _asset_ids(encoded: str) -> tuple[str, ...]:
    values = json.loads(encoded)
    if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
        raise ValueError("invalid asset injection identifiers")
    return tuple(dict.fromkeys(values))


def account_asset_injection(connection: sqlite3.Connection, retrieval_id: str, *, workspace: str) -> None:
    """Count only the final rendered asset event, once, in its INSERT transaction."""
    with _atomic(connection):
        event = connection.execute(
            "SELECT asset_workspace,injected_ids_json,created_at,outcome,scopes_json "
            "FROM retrieval_events WHERE retrieval_id=?", (retrieval_id,),
        ).fetchone()
        if event is None:
            raise ValueError("asset retrieval event missing")
        if event[0] is not None:
            if event[0] != workspace:
                raise ValueError("asset injection workspace mismatch")
            return
        if event[3] != "indexed" or json.loads(event[4]) != ["asset"]:
            return
        ids = _asset_ids(event[1])
        for asset_id in ids:
            updated = connection.execute(
                "UPDATE memory_assets SET injection_count=injection_count+1, "
                "last_injected_at=CASE WHEN last_injected_at IS NULL OR last_injected_at<? "
                "THEN ? ELSE last_injected_at END WHERE workspace=? AND asset_id=?",
                (event[2], event[2], workspace, asset_id),
            )
            if updated.rowcount != 1:
                raise ValueError("injected asset missing from workspace")
        connection.execute(
            "UPDATE retrieval_events SET asset_workspace=? WHERE retrieval_id=?",
            (workspace, retrieval_id),
        )


def _counts(outcome: str | None) -> tuple[int, int]:
    return int(outcome in {"success", "recovered"}), int(outcome == "failure")


def settle_asset_injections(
    connection: sqlite3.Connection, *, workspace: str, trace_id: str, outcome: str | None,
) -> None:
    """Settle each counted event from the shared outcome; corrections transfer counts."""
    if outcome is None:
        return
    if outcome not in TASK_OUTCOMES:
        raise ValueError("asset settlement requires a resolved task outcome")
    with _atomic(connection):
        events = connection.execute(
            "SELECT retrieval_id,injected_ids_json,asset_settled_outcome FROM retrieval_events "
            "WHERE asset_workspace=? AND trace_id=?", (workspace, trace_id),
        ).fetchall()
        for retrieval_id, encoded_ids, old_outcome in events:
            # Late duplicate callbacks from the original failed turn cannot undo
            # a persisted successful correction.
            new_outcome = "recovered" if old_outcome == "recovered" else outcome
            if old_outcome == new_outcome:
                continue
            old_success, old_failure = _counts(old_outcome)
            new_success, new_failure = _counts(new_outcome)
            for asset_id in _asset_ids(encoded_ids):
                updated = connection.execute(
                    "UPDATE memory_assets SET "
                    "post_injection_success_count=post_injection_success_count+?, "
                    "post_injection_failure_count=post_injection_failure_count+? "
                    "WHERE workspace=? AND asset_id=? "
                    "AND post_injection_success_count>=? AND post_injection_failure_count>=?",
                    (new_success-old_success, new_failure-old_failure, workspace, asset_id,
                     old_success-new_success, old_failure-new_failure),
                )
                if updated.rowcount != 1:
                    raise ValueError("asset settlement count invariant violated")
            connection.execute(
                "UPDATE retrieval_events SET asset_settled_outcome=? WHERE retrieval_id=?",
                (new_outcome, retrieval_id),
            )
