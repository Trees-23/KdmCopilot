"""M19 isolated Skill candidate staging.

This repository is intentionally separate from ProposalRepository and from
the active ``skills.current_revision_id`` pointer.  It creates reviewable,
redacted candidate revisions only; M15 evaluation and the existing Proposal /
adoption / Overlay gates remain the only routes toward an active Skill.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from nanobot.memory.stepwise_evidence import TaskEvidence

_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CANDIDATE_STATUSES = frozenset({
    "candidate_staged", "manual_review_required", "evaluating", "rejected_by_quality_gate",
    "insufficient_evidence", "proposal_eligible", "closed",
})


@dataclass(frozen=True, slots=True)
class CandidateStagingRecord:
    candidate_id: str
    workspace: str
    task_id: str
    source_kind: str
    skill_id: str | None
    skill_name: str
    baseline_revision_id: str | None
    candidate_revision_id: str
    baseline_hash: str | None
    candidate_hash: str
    status: str
    reason: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_name(value: str, task_id: str) -> str:
    cleaned = _NAME_RE.sub("-", value.casefold()).strip("-")[:36]
    suffix = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:10]
    return f"evolution-{cleaned or 'task'}-{suffix}"


def _candidate_content(task: TaskEvidence, skill_name: str) -> str:
    projection = task.projection
    goal = projection.task_goal if projection else "已通过人工边界确认的任务"
    intent = projection.intent if projection else "受控任务"
    scope = projection.input_scope if projection else "仅限人工确认范围"
    expected = projection.expected_outcome if projection else "返回可核对结果"
    lines = [
        f"# {skill_name}",
        "",
        "## 解决的问题",
        f"- {goal}",
        "",
        "## 适用场景",
        f"- 任务类型：{intent}",
        f"- 适用范围：{scope}",
        f"- 预期结果：{expected}",
        "",
        "## 执行规则",
        "- 只复用本候选记录的脱敏操作类别，不复放原始命令、完整参数、文件内容、凭据或外部消息。",
    ]
    sequence = 1
    for step in task.steps:
        if step.candidate_use == "reusable":
            action = f"使用 {step.operation_kind}（工具类别：{step.tool_name}），完成后检查 {step.verification_kind or '工具成功状态'}。"
        elif step.candidate_use == "abstract_only":
            action = f"执行受控的 {step.operation_kind} 抽象动作；必须先确认 allowlist、回滚点和人工确认边界。"
        else:
            action = "该步骤仅作为失败/安全审计证据，不得自动重放。"
        lines.append(f"{sequence}. {action} 风险：{step.risk_level}。")
        sequence += 1
    lines.extend([
        "",
        "## 不适用场景",
        "- 目标不明确、超出工作区或资源 allowlist、出现敏感数据、破坏性/不可逆操作时停止并转人工。",
        "- R2/R3 步骤只允许在隔离评测中使用受控模拟或人工交接，不得直接发送、执行或调度。",
        "- 任一 R4 步骤不得进入 Skill 候选执行路径。",
        "",
        "## 沉淀理由",
        "- 目标、步骤顺序和 R0/R1 验证证据已脱敏保存；高风险步骤只保留抽象边界。",
        "",
        "## 来源与验证",
        f"- M18 task：{task.task_id}",
        f"- 来源 Trace 数量：{len(task.source_trace_ids)}",
        f"- M18 资格：{task.qualification}",
        "- 当前版本不会因生成候选而切换；需继续通过 M15 评测与人工 Proposal 门禁。",
    ])
    return "\n".join(lines) + "\n"


def _risk_summary(task: TaskEvidence) -> dict[str, Any]:
    counts: dict[str, int] = {key: 0 for key in ("R0", "R1", "R2", "R3", "R4")}
    uses: dict[str, int] = {key: 0 for key in ("reusable", "abstract_only", "archive_only", "unverified")}
    for step in task.steps:
        counts[step.risk_level] = counts.get(step.risk_level, 0) + 1
        uses[step.candidate_use] = uses.get(step.candidate_use, 0) + 1
    return {"risk_counts": counts, "candidate_use_counts": uses, "qualification": task.qualification}


def _current_baseline(connection: sqlite3.Connection, skill_id: str) -> tuple[str | None, str | None, str | None]:
    row = connection.execute(
        "SELECT current_revision_id FROM skills WHERE skill_id=?", (skill_id,)
    ).fetchone()
    if row is None or not row[0]:
        return None, None, None
    revision = connection.execute(
        "SELECT revision_id,content_hash,content FROM skill_revisions WHERE revision_id=?", (row[0],)
    ).fetchone()
    return (str(revision[0]), str(revision[1]), str(revision[2])) if revision else (None, None, None)


def stage_task_candidate(
    connection: sqlite3.Connection,
    *,
    task: TaskEvidence,
    workspace: str,
    matched_skill_id: str | None = None,
    matched_skill_name: str | None = None,
    match_confidence: float | None = None,
    source_case_ids: Iterable[str] = (),
    allow_partial: bool = False,
) -> CandidateStagingRecord:
    """Stage a new Skill or independent revision without changing activation."""

    if task.workspace != workspace:
        raise ValueError("task evidence belongs to a different workspace")
    if task.qualification != "candidate_eligible" and not (allow_partial and task.qualification == "partial_evidence"):
        raise ValueError("task evidence is not eligible for candidate staging")
    if any(step.risk_level == "R4" for step in task.steps):
        raise ValueError("R4 evidence cannot be staged as a Skill candidate")
    if match_confidence is not None and not 0 <= match_confidence <= 1:
        raise ValueError("match_confidence must be between 0 and 1")
    if match_confidence is not None and 0.4 < match_confidence < 0.8:
        # Uncertain matching deliberately creates an explicit review item,
        # never an automatic revision of an existing Skill.
        skill_name = matched_skill_name or _safe_name(task.projection.intent if task.projection else "task", task.task_id)
        candidate_content = _candidate_content(task, skill_name)
        candidate_hash = _hash(candidate_content)
        candidate_id = f"m19-candidate:{candidate_hash[7:31]}"
        now = _now()
        connection.execute(
            """INSERT OR IGNORE INTO evolution_candidate_staging
               (candidate_id,workspace,task_id,source_kind,skill_id,skill_name,baseline_revision_id,
                candidate_revision_id,baseline_hash,candidate_hash,candidate_content,source_trace_ids_json,
                source_case_ids_json,risk_summary_json,diff_summary,rollback_revision_id,generator_version,
                evidence_schema_version,status,reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate_id, workspace, task.task_id, "skill_candidate", None, skill_name, None,
             candidate_id, None, candidate_hash, candidate_content, json.dumps(task.source_trace_ids),
             json.dumps(tuple(str(item) for item in source_case_ids if str(item))), json.dumps(_risk_summary(task)),
             "匹配不确定，等待人工选择新 Skill 或已有 Skill", None, "m19-v1", "m18-v1",
             "manual_review_required", "skill_match_uncertain", now, now),
        )
        connection.commit()
        return CandidateStagingRecord(candidate_id, workspace, task.task_id, "skill_candidate", None,
                                      skill_name, None, candidate_id, None, candidate_hash,
                                      "manual_review_required", "skill_match_uncertain")

    skill_name = matched_skill_name or _safe_name(task.projection.intent if task.projection else "task", task.task_id)
    candidate_content = _candidate_content(task, skill_name)
    candidate_hash = _hash(candidate_content)
    candidate_id = f"m19-candidate:{candidate_hash[7:31]}"
    existing = matched_skill_id is not None
    baseline_revision_id = baseline_hash = None
    baseline_content = ""
    skill_id = matched_skill_id
    if existing:
        baseline_revision_id, baseline_hash, baseline_content = _current_baseline(connection, matched_skill_id)
        source_kind = "skill_revision_candidate"
    else:
        source_kind = "skill_candidate"
        skill_id = f"skill:m19:{hashlib.sha256(skill_name.encode()).hexdigest()[:24]}"
        connection.execute(
            """INSERT OR IGNORE INTO skills
               (skill_id,namespace,name,source_kind,status,description,created_at,updated_at)
               VALUES(?,?,?,'workspace','staging',?,?,?)""",
            (skill_id, "skill-evolution", skill_name, "M19 candidate", _now(), _now()),
        )
    if baseline_revision_id is None:
        baseline_revision_id = f"m19-baseline:{hashlib.sha256(skill_name.encode()).hexdigest()[:24]}"
        baseline_hash = _hash("")
        connection.execute(
            """INSERT OR IGNORE INTO skill_revisions
               (revision_id,skill_id,skill_version,content_hash,content,author_actor,status,created_at)
               VALUES(?,?,?,?,?,'m19-staging','staging',?)""",
            (baseline_revision_id, skill_id, "0", baseline_hash, "", _now()),
        )
    candidate_revision_id = f"m19-revision:{candidate_hash[7:31]}"
    now = _now()
    connection.execute(
        """INSERT OR IGNORE INTO skill_revisions
           (revision_id,skill_id,skill_version,content_hash,content,previous_revision_id,
            source_case_ids_json,author_actor,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (candidate_revision_id, skill_id, "candidate", candidate_hash, candidate_content,
         baseline_revision_id, json.dumps(tuple(str(item) for item in source_case_ids if str(item))),
         "m19-staging", "staging", now),
    )
    connection.execute(
        """INSERT OR IGNORE INTO evolution_candidate_staging
           (candidate_id,workspace,task_id,source_kind,skill_id,skill_name,baseline_revision_id,
            candidate_revision_id,baseline_hash,candidate_hash,candidate_content,source_trace_ids_json,
            source_case_ids_json,risk_summary_json,diff_summary,rollback_revision_id,generator_version,
            evidence_schema_version,status,reason,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (candidate_id, workspace, task.task_id, source_kind, skill_id, skill_name, baseline_revision_id,
         candidate_revision_id, baseline_hash, candidate_hash, candidate_content,
         json.dumps(task.source_trace_ids), json.dumps(tuple(str(item) for item in source_case_ids if str(item))),
         json.dumps(_risk_summary(task)),
         "新增脱敏行为抽象" if not baseline_content else "基线保留，生成独立修订候选",
         baseline_revision_id, "m19-v1", "m18-v1", "candidate_staged", "", now, now),
    )
    connection.execute(
        """INSERT OR IGNORE INTO evolution_task_links
           (link_id,task_id,source_type,source_id,relation,created_at)
           VALUES(?,?,?,?,?,?)""",
        (f"evolution-link:candidate:{candidate_id}", task.task_id, "candidate", candidate_id, "staged", now),
    )
    connection.commit()
    return CandidateStagingRecord(candidate_id, workspace, task.task_id, source_kind, skill_id,
                                  skill_name, baseline_revision_id, candidate_revision_id,
                                  baseline_hash, candidate_hash, "candidate_staged", "")


def get_staged_candidate(connection: sqlite3.Connection, candidate_id: str) -> CandidateStagingRecord | None:
    row = connection.execute(
        """SELECT candidate_id,workspace,task_id,source_kind,skill_id,skill_name,baseline_revision_id,
                  candidate_revision_id,baseline_hash,candidate_hash,status,reason
           FROM evolution_candidate_staging WHERE candidate_id=?""", (candidate_id,)
    ).fetchone()
    return CandidateStagingRecord(*row) if row else None


def list_staged_candidates(
    connection: sqlite3.Connection,
    *,
    workspace: str,
    statuses: Iterable[str] | None = None,
    limit: int = 50,
) -> tuple[CandidateStagingRecord, ...]:
    if not 1 <= limit <= 100:
        raise ValueError("candidate limit must be between 1 and 100")
    selected = tuple(str(item) for item in (statuses or ()) if str(item))
    query = """SELECT candidate_id,workspace,task_id,source_kind,skill_id,skill_name,baseline_revision_id,
                     candidate_revision_id,baseline_hash,candidate_hash,status,reason
                FROM evolution_candidate_staging WHERE workspace=?"""
    params: list[Any] = [workspace]
    if selected:
        query += " AND status IN (" + ",".join("?" for _ in selected) + ")"
        params.extend(selected)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return tuple(CandidateStagingRecord(*row) for row in connection.execute(query, params).fetchall())


def mark_candidate_status(
    connection: sqlite3.Connection,
    candidate_id: str,
    *,
    status: str,
    reason: str = "",
) -> CandidateStagingRecord | None:
    if status not in _CANDIDATE_STATUSES:
        raise ValueError(f"unsupported candidate status: {status}")
    connection.execute(
        "UPDATE evolution_candidate_staging SET status=?,reason=?,updated_at=? WHERE candidate_id=?",
        (status, reason[:500], _now(), candidate_id),
    )
    connection.commit()
    return get_staged_candidate(connection, candidate_id)


__all__ = [
    "CandidateStagingRecord", "get_staged_candidate", "list_staged_candidates",
    "mark_candidate_status", "stage_task_candidate",
]
