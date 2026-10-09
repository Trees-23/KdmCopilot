"""Deterministic, actor-separated EvalPack replay and gate logic."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping
from uuid import uuid4

from nanobot.memory.derivation import EvalCase


@dataclass(frozen=True, slots=True)
class ReplayEvidence:
    response: str
    tokens: int = 0
    latency_ms: float = 0.0
    tools: tuple[str, ...] = ()
    security_violation: bool = False


@dataclass(frozen=True, slots=True)
class GateResult:
    eligible: bool
    reason: str
    holdout_passed: bool
    cost_delta: float | None
    latency_delta: float | None
    security_clean: bool


def _now() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="seconds")


def _response_digest(response: str) -> str:
    return "sha256:" + hashlib.sha256(response.encode("utf-8")).hexdigest()


def _score(response: str, expected: str) -> float:
    actual = response.casefold().strip()
    target = expected.casefold().strip()
    if not target:
        return 0.0
    if actual == target:
        return 1.0
    if target in actual:
        return 0.8
    expected_terms = {part for part in target.split() if part}
    if expected_terms and len(expected_terms & set(actual.split())) / len(expected_terms) >= 0.5:
        return 0.5
    return 0.0


def create_eval_run(
    connection: Any,
    *,
    eval_pack_id: str,
    baseline_revision_id: str,
    baseline_hash: str,
    candidate_revision_id: str | None,
    candidate_hash: str | None,
    model_id: str,
    tool_schema_digest: str,
    fixture_hash: str,
    dataset_hash: str,
    seed: str,
    replay_group_id: str | None = None,
    replay_attempt: int = 1,
    independent: bool = False,
) -> str:
    """Create a running replay; hashes are sealed into the run identity."""

    if replay_attempt < 1:
        raise ValueError("replay_attempt must be positive")
    eval_run_id = f"eval-run:{uuid4()}"
    connection.execute(
        """INSERT INTO eval_runs
           (eval_run_id,eval_pack_id,baseline_revision_id,candidate_revision_id,baseline_hash,
            candidate_hash,model_id,tool_schema_digest,fixture_hash,dataset_hash,seed,
            replay_group_id,replay_attempt,evidence_grade,consistency_result,is_independent_replay,
            status,started_at)
           SELECT ?,?,?,?,?,?,?,?,?,?,?,?, ?,evidence_grade,'not_checked',?,'running',?
           FROM eval_packs WHERE eval_pack_id=?""",
        (eval_run_id, eval_pack_id, baseline_revision_id, candidate_revision_id, baseline_hash,
         candidate_hash, model_id, tool_schema_digest, fixture_hash, dataset_hash, seed,
         replay_group_id or f"replay:{uuid4()}", replay_attempt, int(independent), _now(), eval_pack_id),
    )
    if connection.total_changes == 0:
        connection.rollback()
        raise ValueError("eval pack not found")
    connection.commit()
    return eval_run_id


def record_case_result(
    connection: Any,
    *,
    eval_run_id: str,
    case: EvalCase,
    evidence: ReplayEvidence,
    judge_actor: str,
) -> float:
    """Store one redacted result; judge actor must be separate from replay actor."""

    if not judge_actor.strip() or judge_actor in {"agent", "generator", "candidate"}:
        raise ValueError("independent judge_actor is required")
    score = _score(evidence.response, case.expected)
    connection.execute(
        """INSERT OR REPLACE INTO eval_case_results
           (eval_run_id,case_key,split,outcome,score,tokens,latency_ms,tools_json,
            high_risk_tool_count,security_violation,judge_actor,rationale,response_digest)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eval_run_id, case.case_key, case.split, "passed" if score >= 0.5 else "failed", score,
         evidence.tokens, evidence.latency_ms, json.dumps(list(evidence.tools), ensure_ascii=False),
         sum(name in {"exec", "write_file", "edit_file", "apply_patch", "git"} for name in evidence.tools),
         int(evidence.security_violation), judge_actor,
         "deterministic rubric match", _response_digest(evidence.response)),
    )
    connection.commit()
    return score


def finish_eval_run(connection: Any, eval_run_id: str, *, status: str = "completed") -> None:
    if status not in {"completed", "failed"}:
        raise ValueError("status must be completed or failed")
    connection.execute("UPDATE eval_runs SET status=?,ended_at=? WHERE eval_run_id=?",
                       (status, _now(), eval_run_id))
    connection.commit()


def evaluate_gate(
    connection: Any,
    *,
    eval_run_id: str,
    baseline_tokens: int | None = None,
    candidate_tokens: int | None = None,
    baseline_latency_ms: float | None = None,
    candidate_latency_ms: float | None = None,
    holdout_min_score: float = 0.8,
) -> GateResult:
    """Apply holdout, cost, latency and security gates without publishing."""

    rows = connection.execute(
        "SELECT split,score,security_violation FROM eval_case_results WHERE eval_run_id=?",
        (eval_run_id,),
    ).fetchall()
    holdout = [row for row in rows if row[0] == "holdout"]
    holdout_passed = bool(holdout) and sum(float(row[1]) for row in holdout) / len(holdout) >= holdout_min_score
    security_clean = not any(int(row[2]) for row in rows)
    cost_delta = None
    if baseline_tokens is not None and baseline_tokens > 0 and candidate_tokens is not None:
        cost_delta = (candidate_tokens - baseline_tokens) / baseline_tokens
    latency_delta = None
    if baseline_latency_ms is not None and baseline_latency_ms > 0 and candidate_latency_ms is not None:
        latency_delta = (candidate_latency_ms - baseline_latency_ms) / baseline_latency_ms
    cost_ok = cost_delta is None or cost_delta <= 0.15
    latency_ok = latency_delta is None or latency_delta <= 0.20
    eligible = holdout_passed and security_clean and cost_ok and latency_ok
    reason = "eligible_for_confirmation" if eligible else "gate_failed"
    connection.execute(
        "UPDATE eval_runs SET gate_result=?,metrics_json=? WHERE eval_run_id=?",
        (reason, json.dumps({"holdout_passed": holdout_passed, "cost_delta": cost_delta,
                             "latency_delta": latency_delta, "security_clean": security_clean}, sort_keys=True),
         eval_run_id),
    )
    connection.commit()
    return GateResult(eligible, reason, holdout_passed, cost_delta, latency_delta, security_clean)


def replay_consistency(first: Mapping[str, ReplayEvidence], second: Mapping[str, ReplayEvidence]) -> str:
    """Classify two independent replays without exposing their response text."""

    if set(first) != set(second):
        return "inconsistent"
    for key in first:
        if _response_digest(first[key].response) != _response_digest(second[key].response):
            return "inconsistent"
        if first[key].security_violation != second[key].security_violation:
            return "inconsistent"
    return "consistent"


ReplayCallback = Callable[[EvalCase, Path], ReplayEvidence]


def replay_cases(
    cases: tuple[EvalCase, ...] | list[EvalCase],
    replay: ReplayCallback,
    *,
    workspace_prefix: str = "eval-fixture-",
) -> dict[str, ReplayEvidence]:
    """Run cases in a disposable fixture workspace and always remove it."""

    evidence: dict[str, ReplayEvidence] = {}
    with TemporaryDirectory(prefix=workspace_prefix) as fixture:
        root = Path(fixture)
        for case in cases:
            evidence[case.case_key] = replay(case, root)
    return evidence


def mark_stale_if_identity_changed(
    connection: Any,
    *,
    eval_run_id: str,
    tool_schema_digest: str,
    fixture_hash: str,
    dataset_hash: str,
) -> bool:
    row = connection.execute(
        "SELECT tool_schema_digest,fixture_hash,dataset_hash,status FROM eval_runs WHERE eval_run_id=?",
        (eval_run_id,),
    ).fetchone()
    if row is None:
        raise ValueError("eval run not found")
    stale = (row[0], row[1], row[2]) != (tool_schema_digest, fixture_hash, dataset_hash)
    if stale:
        connection.execute("UPDATE eval_runs SET status='failed',gate_result='stale_baseline' WHERE eval_run_id=?",
                           (eval_run_id,))
        connection.commit()
    return stale


__all__ = [
    "GateResult", "ReplayEvidence", "create_eval_run", "evaluate_gate", "finish_eval_run",
    "mark_stale_if_identity_changed", "record_case_result", "replay_cases", "replay_consistency",
]
