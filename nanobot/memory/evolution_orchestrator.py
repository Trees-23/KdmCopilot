"""Explicit, fixture-friendly Phase 6 review orchestration.

The orchestrator is intentionally not registered as a Gateway cron job.  A
maintenance/test caller must invoke it explicitly and provide the candidate
revision plus replay fixture.  It can create a Proposal, but never adopts a
Skill, sends QQ messages, creates a PR, or publishes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from nanobot.memory.continuous import (
    EvidenceRecord,
    Phase6RuntimeConfig,
    append_evidence,
    load_state,
    make_proposal,
    persist_proposal,
    select_low_risk_tasks,
)
from nanobot.memory.derivation import (
    EvalCase,
    build_eval_pack_draft,
    derive_case_candidate,
    seal_eval_pack,
)
from nanobot.memory.evaluation import (
    ReplayEvidence,
    create_eval_run,
    evaluate_gate,
    finish_eval_run,
    record_case_result,
    replay_cases,
)
from nanobot.memory.lock import acquire_lock, release_lock

ReplayFixture = Callable[[EvalCase, Path], ReplayEvidence]


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    """All candidate-specific data required by an offline fixture run."""

    task_key: str
    skill_id: str
    skill_name: str
    source_kind: str
    baseline_revision_id: str
    candidate_revision_id: str
    baseline_hash: str
    candidate_hash: str
    cases: tuple[Mapping[str, Any], ...]
    fixture_hash: str
    model_id: str = "fixture-model"
    tool_schema_digest: str = "sha256:fixture-tools"
    replay: ReplayFixture | None = None


@dataclass(frozen=True, slots=True)
class EvolutionCycleResult:
    status: str
    selected_count: int = 0
    staged_case_ids: tuple[str, ...] = ()
    eval_run_ids: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    rejected_task_keys: tuple[str, ...] = ()
    deduplicated_task_keys: tuple[str, ...] = ()
    reason: str | None = None


def _default_replay(case: EvalCase, _fixture_root: Path) -> ReplayEvidence:
    return ReplayEvidence(response=case.expected)


def run_fixture_review_cycle(
    connection: Any,
    workspace: str | Path,
    config: Phase6RuntimeConfig,
    *,
    trace_records: Iterable[Mapping[str, Any]],
    candidate_specs: Sequence[CandidateSpec] = (),
    failed_traces: Iterable[Mapping[str, Any]] = (),
    reviewer: str = "phase6-eval-reviewer",
    actor: str = "phase6-orchestrator",
    owner: str | None = None,
) -> EvolutionCycleResult:
    """Run one bounded offline review cycle under a workspace lease."""

    if not config.enabled or config.kill_switch:
        return EvolutionCycleResult("disabled")
    root = Path(workspace).expanduser().resolve()
    owner = owner or f"phase6-fixture:{uuid4()}"
    lease = acquire_lock(connection, str(root), owner, lease_seconds=120)
    if lease is None:
        return EvolutionCycleResult("locked", reason="workspace_lease_unavailable")
    try:
        state = load_state(root)
        if state.paused:
            return EvolutionCycleResult("paused", reason=state.pause_reason)
        selected = select_low_risk_tasks(trace_records, config)
        selected_keys = {item.task_key for item in selected}
        staged_cases: list[str] = []
        for trace in failed_traces:
            trace_id = str(trace.get("trace_id") or "").strip()
            if not trace_id:
                continue
            case = derive_case_candidate(
                connection,
                trace_id=trace_id,
                intent=str(trace.get("intent") or "trace_regression"),
                task_signature={"source": "phase6", "classification": str(trace.get("outcome") or "unknown")},
                outcome={"status": str(trace.get("outcome") or "unknown"), "summary": str(trace.get("summary") or "")[:500]},
                failure_patterns=tuple(str(value) for value in (trace.get("failure_patterns") or ())) or ("failure",),
                confidence=float(trace.get("confidence") or 0.7),
            )
            staged_cases.append(case.case_id)

        eval_run_ids: list[str] = []
        proposal_ids: list[str] = []
        rejected: list[str] = []
        deduped: list[str] = []
        for spec in candidate_specs:
            if spec.task_key not in selected_keys:
                continue
            eval_pack = build_eval_pack_draft(
                connection,
                skill_id=spec.skill_id,
                skill_revision_id=spec.candidate_revision_id,
                cases=spec.cases,
                fixture_hash=spec.fixture_hash,
                split_policy={"seed": "stable-digest", "phase6": "fixture"},
                actor=actor,
            )
            if not seal_eval_pack(connection, eval_pack.eval_pack_id, reviewer=reviewer):
                rejected.append(spec.task_key)
                continue
            run_id = create_eval_run(
                connection,
                eval_pack_id=eval_pack.eval_pack_id,
                baseline_revision_id=spec.baseline_revision_id,
                baseline_hash=spec.baseline_hash,
                candidate_revision_id=spec.candidate_revision_id,
                candidate_hash=spec.candidate_hash,
                model_id=spec.model_id,
                tool_schema_digest=spec.tool_schema_digest,
                fixture_hash=spec.fixture_hash,
                dataset_hash=eval_pack.dataset_hash,
                seed="fixture-seed",
                independent=True,
            )
            eval_run_ids.append(run_id)
            replay = spec.replay or _default_replay
            evidence = replay_cases(list(eval_pack.cases), replay)
            for case in eval_pack.cases:
                record_case_result(connection, eval_run_id=run_id, case=case,
                                   evidence=evidence[case.case_key], judge_actor=reviewer)
            finish_eval_run(connection, run_id)
            gate = evaluate_gate(connection, eval_run_id=run_id)
            if not gate.eligible:
                rejected.append(spec.task_key)
                continue
            proposal = make_proposal(
                source_kind=spec.source_kind,
                trace_ids=tuple(item.trace_ids for item in selected if item.task_key == spec.task_key)[0],
                case_ids=staged_cases or (f"case:{spec.task_key}",),
                eval_run_ids=(run_id,),
                config=config,
            )
            try:
                persist_proposal(
                    connection,
                    proposal,
                    workspace=str(root),
                    skill_name=spec.skill_name,
                    source_kind=spec.source_kind,
                    baseline_hash=spec.baseline_hash,
                    candidate_hash=spec.candidate_hash,
                    baseline_revision_id=spec.baseline_revision_id,
                    candidate_revision_id=spec.candidate_revision_id,
                    skill_id=spec.skill_id,
                    gate_snapshot={"holdout_passed": gate.holdout_passed, "security_clean": gate.security_clean},
                )
                proposal_ids.append(proposal.proposal_id)
            except Exception as exc:
                if "UNIQUE constraint failed" in str(exc):
                    deduped.append(spec.task_key)
                else:
                    raise
        append_evidence(
            root,
            EvidenceRecord(
                "fixture_review_cycle", "", trace_ids=tuple(item.trace_ids[0] for item in selected if item.trace_ids),
                case_ids=tuple(staged_cases), eval_run_ids=tuple(eval_run_ids),
                status="ok", metrics={"selected": len(selected), "proposals": len(proposal_ids), "rejected": len(rejected)},
            ),
            config,
        )
        return EvolutionCycleResult(
            "completed", len(selected), tuple(staged_cases), tuple(eval_run_ids), tuple(proposal_ids),
            tuple(rejected), tuple(deduped),
        )
    finally:
        release_lock(connection, lease)


__all__ = ["CandidateSpec", "EvolutionCycleResult", "run_fixture_review_cycle"]
