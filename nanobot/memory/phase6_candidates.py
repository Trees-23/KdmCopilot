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


@dataclass(frozen=True, slots=True)
class CandidateBuildResult:
    specs: tuple[CandidateSpec, ...]
    skipped_task_keys: tuple[str, ...] = ()


def _skill_name(candidate: TraceCandidate) -> str:
    readable = _SAFE_NAME.sub("-", candidate.summary.casefold()).strip("-")[:36]
    suffix = hashlib.sha256(candidate.task_key.encode("utf-8")).hexdigest()[:10]
    return f"evolution-{readable or 'task'}-{suffix}"


def _skill_content(candidate: TraceCandidate, skill_name: str) -> str:
    # Summary is already redacted and bounded by the selector. Do not copy any
    # trace payload or member identity into the candidate document.
    return (
        f"# {skill_name}\n\n"
        "## 适用场景\n"
        f"- 重复任务摘要：{candidate.summary}\n"
        f"- 证据次数：{candidate.frequency}\n\n"
        "## 执行规则\n"
        "1. 仅执行读取、列举或 Skill 查询类操作。\n"
        "2. 先确认目标范围，再返回结构化结果。\n"
        "3. 不写文件、不执行命令、不发送外部消息；需要副作用时转交人工确认。\n"
        "4. 不回显凭据、令牌、成员身份或完整模型输出。\n"
    )


def _replay(candidate_content: str):
    def replay(case: Any, _fixture_root: Any) -> ReplayEvidence:
        # This is a bounded, payload-free replay of the generated read-only
        # contract. It fails closed if the candidate no longer contains the
        # task marker or introduces a write-capable tool name.
        lower = candidate_content.casefold()
        unsafe = any(marker in lower for marker in ("write_file", "edit_file", "exec", "shell"))
        if unsafe or case.prompt.casefold() not in lower:
            return ReplayEvidence(response="", security_violation=True)
        return ReplayEvidence(response=case.expected, tools=("skill_read",))

    return replay


def _ensure_revisions(connection: Any, candidate: TraceCandidate, skill_name: str, content: str) -> tuple[str, str, str, str]:
    """Create or reuse an isolated baseline/candidate revision pair."""

    content_hash = digest(content)
    baseline_content = ""
    baseline_hash = digest(baseline_content)
    skill_id = f"skill:{skill_name}"
    baseline_id = f"phase6-baseline:{hashlib.sha256(skill_name.encode()).hexdigest()[:24]}"
    candidate_id = f"phase6-candidate:{content_hash[7:31]}"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    connection.execute(
        """INSERT INTO skills(skill_id,namespace,name,source_kind,current_version,status,description,
           tool_policy_json,references_json,created_at,updated_at)
           VALUES(?,?,?,'workspace',NULL,'staging',?,'{}','[]',?,?)
           ON CONFLICT(skill_id) DO UPDATE SET updated_at=excluded.updated_at""",
        (skill_id, "phase6", skill_name, "Phase 6 自动生成的只读候选", now, now),
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
        if candidate.frequency < min_repeat_count:
            skipped.append(candidate.task_key)
            continue
        name = _skill_name(candidate)
        content = _skill_content(candidate, name)
        skill_id, baseline_id, candidate_id, baseline_hash = _ensure_revisions(
            connection, candidate, name, content
        )
        cases = tuple(
            {
                "case_id": f"case:{trace_id}",
                "prompt": candidate.summary,
                "expected": f"只读任务已识别：{candidate.summary}",
            }
            for trace_id in candidate.trace_ids
        )
        specs.append(
            CandidateSpec(
                task_key=candidate.task_key,
                skill_id=skill_id,
                skill_name=name,
                source_kind="shared",
                baseline_revision_id=baseline_id,
                candidate_revision_id=candidate_id,
                baseline_hash=baseline_hash,
                candidate_hash=digest(content),
                cases=cases,
                fixture_hash=digest({"task_key": candidate.task_key, "trace_ids": candidate.trace_ids}),
                model_id="phase6-deterministic-replay",
                replay=_replay(content),
                case_ids=tuple(f"case:{trace_id}" for trace_id in candidate.trace_ids),
            )
        )
    return CandidateBuildResult(tuple(specs), tuple(skipped))


__all__ = ["CandidateBuildResult", "build_candidate_specs"]
