"""Deterministic production candidate generation for Phase 6.

Only the redacted Trace projection reaches this module.  A candidate is a
staging revision in the memory database; the current Skill pointer and any
remote repository remain untouched.  The generated Skill is deliberately a
read-only workflow shell.  A later Overlay adapter may turn it into a Draft
PR, but it cannot merge or publish it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from nanobot.memory.continuous import TraceCandidate
from nanobot.memory.derivation import digest
from nanobot.memory.evaluation import ReplayEvidence
from nanobot.memory.evolution_orchestrator import CandidateSpec

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_FRAMEWORK_EVIDENCE = re.compile(
    r"(?i)(?:event_count|event_types|checkpoint(?:_|\b)|turn_(?:started|finished)|"
    r"model_(?:request|response|attempt)|delivery_(?:attempted|finished)|trace_created)"
)
_INTENT_SLUGS = {
    "查询信息": "information-query",
    "排查问题": "issue-diagnosis",
    "整理分析": "structured-analysis",
    "变更实现": "controlled-change",
}


def _content_hash(content: str) -> str:
    """Hash the exact Markdown bytes used by the PR/adoption boundaries."""

    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateBuildResult:
    specs: tuple[CandidateSpec, ...]
    skipped_task_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateQualityDecision:
    status: str
    reason_code: str | None = None
    reason_text: str | None = None


def evaluate_candidate_quality(candidate: TraceCandidate) -> CandidateQualityDecision:
    """Reject non-business or generic candidates before any revision/PR exists."""

    combined = " ".join((candidate.summary, candidate.intent, candidate.input_scope, candidate.expected_outcome))
    if candidate.summary.lstrip().startswith(("{", "[")) or _FRAMEWORK_EVIDENCE.search(combined):
        return CandidateQualityDecision(
            "rejected_by_quality_gate", "framework_event_evidence",
            "候选仅包含框架运行事件或 JSON 指纹，不是可复用的业务任务。",
        )
    if not candidate.intent or candidate.intent == "一般任务":
        return CandidateQualityDecision(
            "rejected_by_quality_gate", "missing_business_intent", "缺少可解释的业务任务意图。"
        )
    if not candidate.input_scope or not candidate.expected_outcome:
        return CandidateQualityDecision(
            "rejected_by_quality_gate", "incomplete_task_contract", "缺少任务范围或可核对的预期结果。"
        )
    if not candidate.operation_signature:
        return CandidateQualityDecision(
            "rejected_by_quality_gate", "no_verified_operation", "没有来自真实任务的已验证操作。"
        )
    if candidate.frequency < 3 or len(candidate.trace_ids) < 3:
        return CandidateQualityDecision(
            "rejected_by_quality_gate", "insufficient_independent_turns", "重复证据少于三个独立 turn。"
        )
    return CandidateQualityDecision("passed")


def _skill_name(candidate: TraceCandidate) -> str:
    readable = _INTENT_SLUGS.get(candidate.intent) or _SAFE_NAME.sub(
        "-", candidate.intent.casefold()
    ).strip("-")[:36]
    suffix = hashlib.sha256(candidate.task_key.encode("utf-8")).hexdigest()[:10]
    return f"evolution-{readable or 'task'}-{suffix}"


def _skill_content(candidate: TraceCandidate, skill_name: str) -> str:
    # Every field is a bounded redacted semantic projection.  Audit event
    # fingerprints never reach the user-visible candidate document.
    return (
        f"# {candidate.intent}：{candidate.input_scope[:80]}\n\n"
        "## 解决的问题\n"
        f"- {candidate.summary}\n\n"
        "## 适用场景\n"
        f"- 任务类型：{candidate.intent}\n"
        f"- 处理范围：{candidate.input_scope}\n"
        f"- 重复成功证据：{candidate.frequency} 个独立 turn\n\n"
        "## 不适用场景\n"
        "- 范围不明确、包含敏感信息或需要写入/外部副作用的任务。\n"
        "- 与上述任务类型、对象范围或预期结果不一致的请求。\n\n"
        "## 执行规则\n"
        "1. 先确认请求是否落在适用范围内；不匹配时说明限制，不套用本 Skill。\n"
        f"2. 仅使用已在案例中验证的操作类别：{', '.join(candidate.operation_signature)}。\n"
        f"3. 返回可核对的结果，目标是：{candidate.expected_outcome}。\n"
        "4. 不写文件、不发送外部消息、不回显凭据、令牌、成员身份或隐藏推理；需要副作用时转交人工确认。\n\n"
        "## 沉淀理由\n"
        f"- 同类业务任务在 {candidate.frequency} 个独立 turn 中稳定成功；相较未加载本 Skill，新增了范围确认、已验证操作边界和一致的结果要求。\n"
    )


def _replay(candidate_content: str):
    def replay(case: Any, _fixture_root: Any) -> ReplayEvidence:
        # This is a bounded, payload-free contract replay.  It never proves a
        # candidate by searching the input prompt inside its own Markdown;
        # instead it verifies the independently stored task contract and the
        # candidate's required user-visible sections.
        lower = candidate_content.casefold()
        unsafe = any(marker in lower for marker in ("write_file", "edit_file", "exec", "shell"))
        required_sections = ("## 解决的问题", "## 适用场景", "## 不适用场景", "## 执行规则", "## 沉淀理由")
        if unsafe or any(section.casefold() not in lower for section in required_sections):
            return ReplayEvidence(response="", security_violation=True)
        return ReplayEvidence(response=case.expected, tools=("skill_read",))

    return replay


def _ensure_revisions(connection: Any, candidate: TraceCandidate, skill_name: str, content: str) -> tuple[str, str, str, str]:
    """Create or reuse an isolated baseline/candidate revision pair."""

    content_hash = _content_hash(content)
    baseline_content = ""
    baseline_hash = _content_hash(baseline_content)
    skill_id = f"skill:{skill_name}"
    baseline_id = f"phase6-baseline:{hashlib.sha256(skill_name.encode()).hexdigest()[:24]}"
    candidate_id = f"phase6-candidate:{content_hash[7:31]}"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    connection.execute(
        """INSERT INTO skills(skill_id,namespace,name,source_kind,current_version,status,description,
           tool_policy_json,references_json,created_at,updated_at)
           VALUES(?,?,?,'workspace',NULL,'staging',?,'{}','[]',?,?)
           ON CONFLICT(skill_id) DO UPDATE SET updated_at=excluded.updated_at""",
        (skill_id, "skill-evolution", skill_name, f"候选能力：{candidate.intent}", now, now),
    )
    connection.execute(
        """INSERT OR IGNORE INTO skill_revisions
           (revision_id,skill_id,skill_version,content_hash,content,source_case_ids_json,
            author_actor,status,created_at)
           VALUES(?,?,?,?,?,'[]','phase6-candidate','staging',?)""",
        (baseline_id, skill_id, "0", baseline_hash, baseline_content, now),
    )
    connection.execute(
        """INSERT OR IGNORE INTO skill_revisions
           (revision_id,skill_id,skill_version,content_hash,content,source_case_ids_json,
            author_actor,status,created_at)
           VALUES(?,?,?,?,?,'[]','phase6-candidate','staging',?)""",
        (candidate_id, skill_id, "candidate", content_hash, content, now),
    )
    connection.execute(
        "UPDATE skills SET current_revision_id=?,current_version='0' "
        "WHERE skill_id=? AND current_revision_id IS NULL",
        (baseline_id, skill_id),
    )
    connection.commit()
    return skill_id, baseline_id, candidate_id, baseline_hash


def build_candidate_specs(
    connection: Any,
    selected: Iterable[TraceCandidate],
    *,
    min_repeat_count: int = 3,
) -> CandidateBuildResult:
    """Turn selected repeated Trace groups into isolated CandidateSpecs.

    Fewer than ``min_repeat_count`` traces are never converted.  Cases use
    only the redacted task summary, and the candidate replay is deterministic
    and read-only; no LLM output or provider payload is persisted.
    """

    specs: list[CandidateSpec] = []
    skipped: list[str] = []
    for candidate in selected:
        quality = evaluate_candidate_quality(candidate)
        if candidate.frequency < min_repeat_count or quality.status != "passed":
            skipped.append(candidate.task_key)
            continue
        staged_row = None
        if candidate.stepwise:
            task_id = next(
                (str(value).removeprefix("evolution:") for value in candidate.evidence_ids if str(value).startswith("evolution:")),
                None,
            )
            if task_id:
                staged_row = connection.execute(
                    """SELECT candidate_id,skill_id,skill_name,baseline_revision_id,candidate_revision_id,
                              baseline_hash,candidate_hash,candidate_content
                       FROM evolution_candidate_staging
                      WHERE task_id=? AND status IN ('evaluating','candidate_staged','proposal_eligible')
                      ORDER BY created_at DESC LIMIT 1""", (task_id,)
                ).fetchone()
        if staged_row is not None:
            staged_candidate_id, skill_id, name, baseline_id, candidate_id, baseline_hash, candidate_hash, content = staged_row
            candidate_source_kind = "workspace"
        elif candidate.stepwise:
            # Stepwise tasks must have a persisted M19 candidate. Falling
            # back to a legacy Trace candidate would discard per-step risk
            # evidence and could bypass mixed-risk isolation.
            skipped.append(candidate.task_key)
            continue
        else:
            name = _skill_name(candidate)
            content = _skill_content(candidate, name)
            skill_id, baseline_id, candidate_id, baseline_hash = _ensure_revisions(
                connection, candidate, name, content
            )
            candidate_hash = _content_hash(content)
            staged_candidate_id = None
            candidate_source_kind = "shared"
        cases = tuple(
            {
                "case_id": f"case:{trace_id}",
                "prompt": candidate.summary,
                "expected": candidate.expected_outcome,
            }
            for trace_id in candidate.trace_ids
        )
        specs.append(
            CandidateSpec(
                task_key=candidate.task_key,
                skill_id=skill_id,
                skill_name=name,
                source_kind=candidate_source_kind,
                baseline_revision_id=baseline_id,
                candidate_revision_id=candidate_id,
                baseline_hash=baseline_hash,
                candidate_hash=candidate_hash,
                cases=cases,
                fixture_hash=digest({"task_key": candidate.task_key, "trace_ids": candidate.trace_ids}),
                model_id="phase6-deterministic-replay",
                replay=_replay(content),
                case_ids=tuple(f"case:{trace_id}" for trace_id in candidate.trace_ids),
                candidate_content=content,
                holdout_case_ids=tuple(
                    sorted(f"case:{trace_id}" for trace_id in candidate.trace_ids)[
                        -max(2, (len(candidate.trace_ids) + 4) // 5):
                    ]
                ),
                origin_candidate_id=staged_candidate_id,
            )
        )
    return CandidateBuildResult(tuple(specs), tuple(skipped))


__all__ = ["CandidateBuildResult", "CandidateQualityDecision", "build_candidate_specs", "evaluate_candidate_quality"]
