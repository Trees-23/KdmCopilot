"""Atomic content admission and daily accounting before model requests are issued."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.providers.observed_call import ProviderCallAdmissionError

PROTOCOL_VERSION = "two-round-v1"
MINIMUM_COMPLETE_RUN_CALLS = 40
_BEIJING = ZoneInfo("Asia/Shanghai")


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _day(value: datetime) -> str:
    return value.astimezone(_BEIJING).date().isoformat()


class EvaluationBudgetError(ProviderCallAdmissionError):
    """The request was rejected before dispatch, so no further model charge is made."""


@dataclass(frozen=True, slots=True)
class EvaluationAdmission:
    status: str
    evaluation_id: str | None = None
    result_status: str | None = None
    reason: str | None = None


def _usage(connection: sqlite3.Connection, workspace: str, now: datetime) -> tuple[int, int]:
    day = _day(now)
    start = datetime.fromisoformat(day).replace(tzinfo=_BEIJING).astimezone(UTC)
    end = start + timedelta(days=1)
    candidates = connection.execute(
        "SELECT count(*) FROM (SELECT candidate_hash FROM evaluation_candidate_admissions WHERE workspace=? AND budget_day=? "
        "UNION SELECT candidate_hash FROM ab_evaluations WHERE workspace=? AND julianday(created_at)>=julianday(?) "
        "AND julianday(created_at)<julianday(?) AND protocol_version<>?)",
        (workspace, day, workspace, _stamp(start), _stamp(end), PROTOCOL_VERSION),
    ).fetchone()[0]
    calls = connection.execute(
        "SELECT coalesce(sum(actual_calls+reserved_calls+unconfirmed_calls),0) "
        "FROM evaluation_model_call_reservations WHERE workspace=? AND budget_day=?",
        (workspace, day),
    ).fetchone()[0]
    # Old single-round evidence is not a valid two-round verdict, but its actual
    # same-day requests still consume the workspace's daily budget.
    legacy = connection.execute(
        "SELECT count(DISTINCT candidate_hash),coalesce(sum(model_calls_used),0) FROM ab_evaluations "
        "WHERE workspace=? AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) AND protocol_version<>?",
        (workspace, _stamp(start), _stamp(end), PROTOCOL_VERSION),
    ).fetchone()
    return int(candidates), int(calls + legacy[1])


def _expire_executions(connection: sqlite3.Connection, workspace: str, now: datetime) -> None:
    rows = connection.execute(
        "SELECT evaluation_id FROM ab_evaluations WHERE workspace=? AND protocol_version=? "
        "AND status='running' AND execution_deadline<=?",
        (workspace, PROTOCOL_VERSION, _stamp(now)),
    ).fetchall()
    for row in rows:
        # After a crash, unused capacity is conservatively held, never fabricated
        # as an observed request and never returned to the daily allowance.
        connection.execute(
            "UPDATE evaluation_model_call_reservations SET unconfirmed_calls=unconfirmed_calls+reserved_calls,"
            "reserved_calls=0,status='abandoned' WHERE evaluation_id=? AND status='active'",
            (row[0],),
        )
        connection.execute(
            "UPDATE ab_evaluations SET status='error',reason_code='lease_expired',completed_at=? "
            "WHERE evaluation_id=? AND status='running'",
            (_stamp(now), row[0]),
        )


def _has_daily_slot(connection: sqlite3.Connection, workspace: str, candidate_hash: str, now: datetime) -> bool:
    day = _day(now)
    start = datetime.fromisoformat(day).replace(tzinfo=_BEIJING).astimezone(UTC)
    return connection.execute(
        "SELECT 1 FROM evaluation_candidate_admissions WHERE workspace=? AND budget_day=? AND candidate_hash=? "
        "UNION SELECT 1 FROM ab_evaluations WHERE workspace=? AND candidate_hash=? AND protocol_version<>? "
        "AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) LIMIT 1",
        (workspace, day, candidate_hash, workspace, candidate_hash, PROTOCOL_VERSION,
         _stamp(start), _stamp(start + timedelta(days=1))),
    ).fetchone() is not None


def admit_evaluation(
    connection: sqlite3.Connection,
    workspace: str,
    spec: Any,
    config: Phase6RuntimeConfig,
    *,
    required_calls: int = MINIMUM_COMPLETE_RUN_CALLS,
) -> EvaluationAdmission:
    """Check content idempotency and reserve one candidate slot plus model capacity."""
    if required_calls < 0:
        raise ValueError("required_calls must be non-negative")
    now = _now()
    day = _day(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        _expire_executions(connection, workspace, now)
        previous = connection.execute(
            "SELECT evaluation_id,status,reason_code FROM ab_evaluations "
            "WHERE workspace=? AND candidate_hash=? AND protocol_version=? AND status IN ('passed','failed')",
            (workspace, spec.candidate_hash, PROTOCOL_VERSION),
        ).fetchone()
        if previous is not None:
            connection.commit()
            return EvaluationAdmission("cached", str(previous[0]), str(previous[1]), str(previous[2]))
        active = connection.execute(
            "SELECT evaluation_id FROM ab_evaluations WHERE workspace=? AND candidate_hash=? "
            "AND protocol_version=? AND status='running'",
            (workspace, spec.candidate_hash, PROTOCOL_VERSION),
        ).fetchone()
        if active:
            connection.commit()
            return EvaluationAdmission("running", str(active[0]))
        attempts = connection.execute(
            "SELECT evaluation_id,status FROM ab_evaluations WHERE workspace=? AND candidate_hash=? "
            "AND protocol_version=? ORDER BY created_at DESC,evaluation_id DESC",
            (workspace, spec.candidate_hash, PROTOCOL_VERSION),
        ).fetchall()
        if len(attempts) >= config.evaluation_max_attempts:
            reason = "judge_unstable" if all(row[1] == "uncertain" for row in attempts[:3]) else "attempts_exhausted"
            connection.execute(
                "UPDATE ab_evaluations SET status='failed',reason_code=?,completed_at=? WHERE evaluation_id=?",
                (reason, _stamp(now), attempts[0][0]),
            )
            connection.execute(
                "UPDATE evaluation_candidate_admissions SET settled=1 WHERE workspace=? AND candidate_hash=?",
                (workspace, spec.candidate_hash),
            )
            connection.commit()
            return EvaluationAdmission("cached", str(attempts[0][0]), "failed", reason)
        candidates, calls = _usage(connection, workspace, now)
        existing_slot = _has_daily_slot(connection, workspace, spec.candidate_hash, now)
        if ((not existing_slot and candidates >= config.ab_evaluation_max_candidates_per_day)
                or calls + required_calls > config.ab_evaluation_max_model_calls_per_day):
            connection.commit()
            return EvaluationAdmission("budget_exhausted", reason="ab_evaluation_daily_budget_exhausted")
        evaluation_id = f"ab-eval:{uuid4()}"
        connection.execute(
            "INSERT INTO ab_evaluations(evaluation_id,workspace,skill_name,candidate_hash,mode,model_id,"
            "reasoning_effort,real_evidence_count,max_model_calls,status,reason_code,created_at,"
            "protocol_version,execution_deadline) VALUES(?,?,?,?,?,?,?,?,?,'running','started',?,?,?)",
            (evaluation_id, workspace, spec.skill_name, spec.candidate_hash, config.ab_evaluation_mode,
             config.ab_evaluation_model, config.ab_evaluation_reasoning_effort, len(set(spec.case_ids)),
             config.ab_evaluation_max_model_calls_per_day, _stamp(now), PROTOCOL_VERSION,
             _stamp(now + timedelta(seconds=config.evaluation_timeout_seconds))),
        )
        connection.execute(
            "INSERT OR IGNORE INTO evaluation_candidate_admissions "
            "(workspace,budget_day,candidate_hash,admitted_evaluation_id) VALUES(?,?,?,?)",
            (workspace, day, spec.candidate_hash, evaluation_id),
        )
        connection.execute(
            "INSERT INTO evaluation_model_call_reservations "
            "(evaluation_id,budget_day,workspace,reserved_calls,status) VALUES(?,?,?,?,'active')",
            (evaluation_id, day, workspace, required_calls),
        )
        connection.commit()
        return EvaluationAdmission("admitted", evaluation_id)
    except BaseException:
        connection.rollback()
        raise


@dataclass(slots=True)
class EvaluationBudget:
    connection: sqlite3.Connection
    workspace: str
    evaluation_id: str
    config: Phase6RuntimeConfig

    def charge_request(self) -> None:
        """Atomically debit a real attempt, including retries, before network dispatch."""
        now = _now()
        day = _day(now)
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            execution = connection.execute(
                "SELECT status,execution_deadline,candidate_hash FROM ab_evaluations WHERE evaluation_id=?",
                (self.evaluation_id,),
            ).fetchone()
            if not execution or execution[0] != "running" or execution[1] <= _stamp(now):
                raise EvaluationBudgetError("evaluation_execution_expired")
            row = connection.execute(
                "SELECT reserved_calls,status FROM evaluation_model_call_reservations "
                "WHERE evaluation_id=? AND budget_day=?",
                (self.evaluation_id, day),
            ).fetchone()
            if row is None:
                # A long execution may cross Beijing midnight. Keep already-used
                # calls on their original day and admit its remaining work anew.
                remaining = max(1, connection.execute(
                    "SELECT coalesce(sum(reserved_calls),0) FROM evaluation_model_call_reservations "
                    "WHERE evaluation_id=? AND status='active'", (self.evaluation_id,),
                ).fetchone()[0])
                candidates, calls = _usage(connection, self.workspace, now)
                slot = _has_daily_slot(connection, self.workspace, execution[2], now)
                if ((not slot and candidates >= self.config.ab_evaluation_max_candidates_per_day)
                        or calls + remaining > self.config.ab_evaluation_max_model_calls_per_day):
                    raise EvaluationBudgetError("evaluation_daily_budget_exhausted")
                connection.execute(
                    "UPDATE evaluation_model_call_reservations SET reserved_calls=0,status='settled' "
                    "WHERE evaluation_id=? AND status='active'", (self.evaluation_id,),
                )
                connection.execute(
                    "INSERT INTO evaluation_model_call_reservations "
                    "(evaluation_id,budget_day,workspace,reserved_calls,status) VALUES(?,?,?,?,'active')",
                    (self.evaluation_id, day, self.workspace, remaining),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO evaluation_candidate_admissions "
                    "(workspace,budget_day,candidate_hash,admitted_evaluation_id) VALUES(?,?,?,?)",
                    (self.workspace, day, execution[2], self.evaluation_id),
                )
                row = (remaining, "active")
            if row[1] != "active":
                raise EvaluationBudgetError("evaluation_execution_expired")
            if row[0] <= 0:
                _candidates, calls = _usage(connection, self.workspace, now)
                if calls >= self.config.ab_evaluation_max_model_calls_per_day:
                    raise EvaluationBudgetError("evaluation_daily_budget_exhausted")
            connection.execute(
                "UPDATE evaluation_model_call_reservations SET reserved_calls=max(0,reserved_calls-1),"
                "actual_calls=actual_calls+1 WHERE evaluation_id=? AND budget_day=? AND status='active'",
                (self.evaluation_id, day),
            )
            connection.execute(
                "UPDATE ab_evaluations SET model_calls_used=model_calls_used+1 WHERE evaluation_id=?",
                (self.evaluation_id,),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def used_calls(self) -> int:
        return int(self.connection.execute(
            "SELECT model_calls_used FROM ab_evaluations WHERE evaluation_id=?", (self.evaluation_id,),
        ).fetchone()[0])

    def settle(self, status: str, reason: str, metrics: dict[str, Any] | None = None) -> bool:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT status,execution_deadline,candidate_hash FROM ab_evaluations WHERE evaluation_id=?",
                (self.evaluation_id,),
            ).fetchone()
            if not row or row[0] != "running":
                connection.rollback()
                return False
            if status in {"passed", "failed"} and reason != "evaluation_timeout" and row[1] <= _stamp(_now()):
                status, reason = "error", "lease_expired"
            connection.execute(
                "UPDATE ab_evaluations SET status=?,reason_code=?,metrics_json=?,completed_at=? WHERE evaluation_id=?",
                (status, reason, json.dumps(metrics or {}, ensure_ascii=False, sort_keys=True),
                 _stamp(_now()), self.evaluation_id),
            )
            connection.execute(
                "UPDATE evaluation_model_call_reservations SET reserved_calls=0,status='settled' "
                "WHERE evaluation_id=? AND status='active'", (self.evaluation_id,),
            )
            if status in {"passed", "failed"}:
                connection.execute(
                    "UPDATE evaluation_candidate_admissions SET settled=1 WHERE workspace=? AND candidate_hash=?",
                    (self.workspace, row[2]),
                )
            connection.commit()
            return status != "error" or reason != "lease_expired"
        except BaseException:
            connection.rollback()
            raise

    def discard_unstarted(self) -> None:
        """Return a reservation if no queue lease was acquired and no request was issued."""
        self.connection.execute(
            "DELETE FROM ab_evaluations WHERE evaluation_id=? AND status='running' AND model_calls_used=0 "
            "AND NOT EXISTS (SELECT 1 FROM evaluation_model_call_reservations r WHERE "
            "r.evaluation_id=ab_evaluations.evaluation_id AND (r.status='abandoned' OR r.unconfirmed_calls>0))",
            (self.evaluation_id,),
        )
        self.connection.commit()
