"""Phase 5 derivation helpers for Case candidates and sealed EvalPack drafts.

This module only writes derived candidate/evaluation metadata.  Audit events,
Wiki Markdown and the current Skill revision remain owned by their providers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_PARTIAL = "partial"
OUTCOME_BLOCKED = "blocked"
OUTCOME_UNKNOWN = "unknown"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def classify_outcome(outcome: str | None, *, error: bool = False) -> str:
    """Normalize trace outcomes without retaining provider payloads."""

    value = str(outcome or "").casefold()
    if error or value in {"failed", "failure", "error", "exception"}:
        return OUTCOME_FAILURE
    if value in {"blocked", "denied", "policy_blocked", "cancelled", "canceled"}:
        return OUTCOME_BLOCKED
    if value in {"partial", "degraded", "timeout", "timed_out"}:
        return OUTCOME_PARTIAL
    if value in {"success", "succeeded", "completed", "ok", "done"}:
        return OUTCOME_SUCCESS
    return OUTCOME_UNKNOWN


@dataclass(frozen=True, slots=True)
class CaseCandidate:
    case_id: str
    page_id: str
    trace_id: str
    intent: str
    task_signature: dict[str, Any]
    outcome: dict[str, Any]
    failure_patterns: tuple[str, ...]
    confidence: float
    status: str = "candidate"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def derive_case_candidate(
    connection: Any,
    *,
    trace_id: str,
    intent: str,
    task_signature: Mapping[str, Any],
    outcome: Mapping[str, Any] | None = None,
    failure_patterns: Sequence[str] = (),
    page_id: str | None = None,
    source_case_ids: Sequence[str] = (),
    confidence: float = 0.5,
) -> CaseCandidate:
    """Create or replace one staging Case candidate from a Trace summary.

    The candidate contains only caller-supplied structured summaries.  If no
    provider page exists, a derived ``wiki_pages`` index row is created with a
    non-authoritative ``derived://`` source path.
    """

    if not trace_id.strip() or not intent.strip():
        raise ValueError("trace_id and intent are required")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    payload = dict(outcome or {})
    payload.setdefault("classification", classify_outcome(payload.get("status")))
    page_id = page_id or f"case-page:{trace_id}"
    case_id = f"case:{trace_id}"
    now = _now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """INSERT INTO wiki_pages
               (page_id,namespace,slug,page_type,source_path,content_hash,current_revision_id,
                status,sensitivity,title,summary,tags_json,source_refs_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(page_id) DO UPDATE SET content_hash=excluded.content_hash,
                status='candidate',summary=excluded.summary,updated_at=excluded.updated_at""",
            (
                page_id, "workspace", f"case/{trace_id}", "case", f"derived://trace/{trace_id}",
                digest({"trace_id": trace_id, "task_signature": dict(task_signature)}), None,
                "candidate", "private", intent, json.dumps(payload, ensure_ascii=False),
                "[]", json.dumps(list(source_case_ids), ensure_ascii=False), now, now,
            ),
        )
        connection.execute(
            """INSERT INTO cases
               (case_id,page_id,skill_id,skill_version,intent,task_signature_json,preconditions_json,
                steps_json,outcome_json,failure_patterns_json,trace_ids_json,confidence,user_confirmed,
                status,revision_id,previous_revision_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,'candidate',?,?,?,?)
               ON CONFLICT(case_id) DO UPDATE SET intent=excluded.intent,
                task_signature_json=excluded.task_signature_json,outcome_json=excluded.outcome_json,
                failure_patterns_json=excluded.failure_patterns_json,confidence=excluded.confidence,
                trace_ids_json=excluded.trace_ids_json,status='candidate',updated_at=excluded.updated_at""",
            (
                case_id, page_id, None, None, intent, _canonical(dict(task_signature)), "[]", "[]",
                _canonical(payload), _canonical(list(failure_patterns)), _canonical([trace_id]), confidence,
                digest({"case_id": case_id, "trace_id": trace_id, "revision": now}), None, now, now,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return CaseCandidate(case_id, page_id, trace_id, intent, dict(task_signature), payload,
                         tuple(failure_patterns), confidence)


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_key: str
    prompt: str
    expected: str
    split: str
    source_case_id: str


@dataclass(frozen=True, slots=True)
class EvalPackDraft:
    eval_pack_id: str
    skill_id: str
    skill_revision_id: str
    dataset_hash: str
    fixture_hash: str
    question_count: int
    valid_count: int
    evidence_grade: str
    cases: tuple[EvalCase, ...]


def _split_for(index: int, total: int) -> str:
    if total >= 20:
        target = (10, 5, 5)
    elif total >= 10:
        target = (max(1, total // 2), max(1, (total - total // 2) // 2), 0)
        target = (target[0], target[1], total - target[0] - target[1])
    else:
        target = (total, 0, 0)
    if index < target[0]:
        return "train"
    if index < target[0] + target[1]:
        return "validation"
    return "holdout"


def build_eval_pack_draft(
    connection: Any,
    *,
    skill_id: str,
    skill_revision_id: str,
    cases: Iterable[Mapping[str, Any]],
    fixture_hash: str,
    rubric: Mapping[str, Any] | None = None,
    split_policy: Mapping[str, Any] | None = None,
    actor: str = "derivation",
) -> EvalPackDraft:
    """Build a deterministic EvalPack draft; no current revision is changed."""

    if not fixture_hash:
        raise ValueError("fixture_hash is required")
    normalized: list[dict[str, Any]] = []
    for item in cases:
        key = str(item.get("case_key") or item.get("case_id") or "").strip()
        prompt = str(item.get("prompt") or item.get("question") or "").strip()
        expected = str(item.get("expected") or item.get("expected_outcome") or "").strip()
        if key and prompt and expected:
            normalized.append({"case_key": key, "prompt": prompt, "expected": expected,
                               "source_case_id": str(item.get("case_id") or key)})
    normalized.sort(key=lambda item: digest(item))
    total = len(normalized)
    grade = "standard" if total >= 20 else "limited" if total >= 10 else "insufficient_evidence"
    eval_cases = tuple(EvalCase(item["case_key"], item["prompt"], item["expected"],
                                _split_for(index, total), item["source_case_id"])
                       for index, item in enumerate(normalized))
    dataset_hash = digest([item.__dict__ if hasattr(item, "__dict__") else {
        "case_key": item.case_key, "prompt": item.prompt, "expected": item.expected,
        "split": item.split, "source_case_id": item.source_case_id,
    } for item in eval_cases])
    eval_pack_id = f"eval-pack:{uuid4()}"
    now = _now()
    connection.execute(
        """INSERT INTO eval_packs
           (eval_pack_id,skill_id,skill_revision_id,dataset_hash,fixture_hash,rubric_json,
            split_policy_json,source_manifest_json,question_count,valid_count,evidence_grade,
            status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?)""",
        (eval_pack_id, skill_id, skill_revision_id, dataset_hash, fixture_hash,
         _canonical(dict(rubric or {})), _canonical(dict(split_policy or {"seed": "stable-digest"})),
         _canonical({"actor": actor, "case_ids": [item.source_case_id for item in eval_cases]}),
         total, total, grade, now),
    )
    connection.commit()
    return EvalPackDraft(eval_pack_id, skill_id, skill_revision_id, dataset_hash, fixture_hash,
                         total, total, grade, eval_cases)


def seal_eval_pack(connection: Any, eval_pack_id: str, *, reviewer: str) -> bool:
    """Seal a draft using an independent reviewer actor."""

    if not reviewer.strip() or reviewer in {"derivation", "agent", "generator"}:
        raise ValueError("an independent reviewer is required")
    now = _now()
    cursor = connection.execute(
        """UPDATE eval_packs SET status='sealed',sealed_by=?,sealed_at=?
           WHERE eval_pack_id=? AND status='draft'""", (reviewer, now, eval_pack_id)
    )
    connection.commit()
    return cursor.rowcount == 1


__all__ = [
    "CaseCandidate", "EvalCase", "EvalPackDraft", "OUTCOME_BLOCKED", "OUTCOME_FAILURE",
    "OUTCOME_PARTIAL", "OUTCOME_SUCCESS", "OUTCOME_UNKNOWN", "build_eval_pack_draft",
    "classify_outcome", "derive_case_candidate", "digest", "seal_eval_pack",
]
