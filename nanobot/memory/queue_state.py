"""Candidate queue transitions owned by bounded execution leases."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

QUEUE_STATES = frozenset({
    "queued", "manual_review", "evaluating", "failed",
    "proposal_eligible", "adopted", "closed", "archived",
})
MAX_ATTEMPTS = 3


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class EvaluationLease:
    candidate_id: str
    workspace: str
    owner: str
    attempt: int
    expires_at: str


def reap_stale_evaluations(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    now: datetime | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> int:
    """Recover crashed executions; an exhausted candidate never returns to the queue."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    timestamp = _timestamp(now)
    cursor = connection.execute(
        """UPDATE evolution_candidate_staging
           SET status=CASE WHEN attempt_count>=? THEN 'failed' ELSE 'queued' END,
               lease_owner=NULL,lease_expires_at=NULL,last_error='lease_expired',updated_at=?
           WHERE workspace=? AND status='evaluating'
             AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at<=?)""",
        (max_attempts, timestamp, workspace, timestamp),
    )
    connection.commit()
    return cursor.rowcount


def claim_candidate(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    owner: str,
    candidate_id: str | None = None,
    lease_seconds: int = 900,
    max_attempts: int = MAX_ATTEMPTS,
    now: datetime | None = None,
) -> EvaluationLease | None:
    """Atomically claim one queued candidate in FIFO order, counting every execution."""
    if not workspace or not owner or lease_seconds < 1 or max_attempts < 1:
        raise ValueError("workspace, owner and positive lease/attempt limits are required")
    current = now or datetime.now(UTC)
    timestamp = _timestamp(current)
    expires_at = _timestamp(current + timedelta(seconds=lease_seconds))
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT candidate_id,attempt_count FROM evolution_candidate_staging "
            "WHERE workspace=? AND status='queued' AND attempt_count<? "
            "AND (? IS NULL OR candidate_id=?) ORDER BY created_at,candidate_id LIMIT 1",
            (workspace, max_attempts, candidate_id, candidate_id),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        cursor = connection.execute(
            """UPDATE evolution_candidate_staging
               SET status='evaluating',lease_owner=?,lease_expires_at=?,
                   attempt_count=attempt_count+1,updated_at=?
               WHERE candidate_id=? AND workspace=? AND status='queued' AND attempt_count=?""",
            (owner, expires_at, timestamp, row[0], workspace, row[1]),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        connection.commit()
        return EvaluationLease(str(row[0]), workspace, owner, int(row[1]) + 1, expires_at)
    except BaseException:
        connection.rollback()
        raise


def finish_evaluation(
    connection: sqlite3.Connection,
    lease: EvaluationLease,
    *,
    status: str,
    reason: str = "",
    last_error: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    now: datetime | None = None,
) -> bool:
    """Release only a still-valid lease; late and superseded workers cannot write results."""
    if status not in {"queued", "failed", "proposal_eligible"}:
        raise ValueError("evaluation must finish as queued, failed or proposal_eligible")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if status == "queued" and lease.attempt >= max_attempts:
        status, last_error = "failed", last_error or "attempts_exhausted"
    timestamp = _timestamp(now)
    cursor = connection.execute(
        """UPDATE evolution_candidate_staging
           SET status=?,reason=?,last_error=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=?
           WHERE candidate_id=? AND workspace=? AND status='evaluating'
             AND lease_owner=? AND attempt_count=? AND lease_expires_at=? AND lease_expires_at>?""",
        (status, reason[:500], last_error, timestamp, lease.candidate_id, lease.workspace,
         lease.owner, lease.attempt, lease.expires_at, timestamp),
    )
    connection.commit()
    return cursor.rowcount == 1


def transition_candidate(
    connection: sqlite3.Connection,
    candidate_id: str,
    *,
    status: str,
    reason: str = "",
) -> bool:
    """Apply explicit non-execution transitions without allowing failed candidates to requeue."""
    sources = {
        "queued": ("manual_review",),
        "adopted": ("proposal_eligible",),
        "closed": ("queued", "manual_review", "evaluating", "proposal_eligible", "adopted"),
        "archived": ("queued", "manual_review", "evaluating", "proposal_eligible", "adopted", "failed"),
    }
    if status not in QUEUE_STATES:
        raise ValueError("unsupported queue state")
    if status not in sources:
        raise ValueError("execution state changes require an evaluation lease")
    previous = sources[status]
    cursor = connection.execute(
        "UPDATE evolution_candidate_staging SET status=?,reason=?,lease_owner=NULL,"
        "lease_expires_at=NULL,updated_at=? WHERE candidate_id=? AND status IN ("
        + ",".join("?" for _ in previous) + ")",
        (status, reason[:500], _timestamp(), candidate_id, *previous),
    )
    connection.commit()
    return cursor.rowcount == 1
