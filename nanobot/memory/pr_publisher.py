"""Create a local branch and Draft PR description for an approved Skill Proposal.

This adapter deliberately stops before any remote operation.  It prepares the
reviewable Git change and records the Proposal transition to ``pr_created``;
GitHub creation, merge, deployment, and Skill pointer changes belong to M8.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

from nanobot.memory.continuous import phase6_active
from nanobot.memory.proposal_repository import ProposalConflict, ProposalRepository

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ALLOWED_SOURCES = frozenset({"builtin", "shared", "entrypoint", "mcp"})
_RUN = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class DraftPRResult:
    """Outcome of preparing a local Draft PR change."""

    status: str
    proposal_id: str
    branch: str | None = None
    commit: str | None = None
    title: str | None = None
    body: str | None = None
    path: str | None = None
    reason: str | None = None


def _evolution(config: Any) -> Any:
    return getattr(config, "evolution", config)


def _enabled(config: Any) -> bool:
    # M7 uses the same explicit human-approval gate as workspace adoption.  The
    # publish gate is intentionally not consulted: creating a Draft PR is not
    # publishing it.
    return phase6_active(config) and bool(getattr(_evolution(config), "adoption_enabled", False))


def _safe_component(value: str, label: str) -> str:
    if not _SAFE_NAME.fullmatch(value):
        raise ValueError(f"unsafe {label}")
    return value


def _branch_name(proposal_id: str) -> str:
    # Proposal IDs are generated internally, but keep the branch name bounded
    # and deterministic in case an imported Proposal contains punctuation.
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", proposal_id).strip("-.")
    if not clean:
        raise ValueError("unsafe proposal id")
    return f"evolve/{clean[:80]}"


def _run_git(run: _RUN, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=repo, check=False, capture_output=True, text=True)


def _git_error(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "git command failed").strip()[:300]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _load_proposal(connection: sqlite3.Connection, proposal_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT p.workspace,p.skill_id,p.skill_name,p.source_kind,p.target,p.status,"
        "p.baseline_revision_id,p.candidate_revision_id,p.baseline_hash,p.candidate_hash,"
        "p.version_epoch,p.gate_result,p.trace_ids_json,p.case_ids_json,p.eval_run_ids_json,"
        "s.current_revision_id,r.content,r.content_hash "
        "FROM skill_proposals p LEFT JOIN skills s ON s.skill_id=p.skill_id "
        "LEFT JOIN skill_revisions r ON r.revision_id=p.candidate_revision_id "
        "WHERE p.proposal_id=?",
        (proposal_id,),
    ).fetchone()


def _make_body(row: sqlite3.Row, *, branch: str, commit: str) -> str:
    # Only stable identifiers, counts and digests are included.  In particular,
    # no chat text, model output, member identity, token, or secret is copied.
    trace_ids = json.loads(row[12] or "[]")
    case_ids = json.loads(row[13] or "[]")
    eval_ids = json.loads(row[14] or "[]")
    return (
        "## 改动内容\n\n"
        f"- Skill：`{row[2]}`（来源：`{row[3]}`）\n"
        f"- 候选文件：`skills/{row[2]}/SKILL.md`\n"
        f"- 基线版本：`{row[6] or '未提供'}`\n"
        f"- 候选版本：`{row[7] or '未提供'}`\n"
        f"- 基线摘要：`{row[8]}`\n"
        f"- 候选摘要：`{row[9]}`\n\n"
        "## 评测证据\n\n"
        f"- 自动评审：`{row[11]}`\n"
        f"- Trace：{len(trace_ids)} 条；Case：{len(case_ids)} 个；EvalRun：{len(eval_ids)} 次\n"
        f"- 本地分支：`{branch}`\n"
        f"- 提交：`{commit}`\n\n"
        "## 验证结果\n\n"
        "- 候选内容已按 Proposal 候选摘要校验。\n"
        "- 仅创建专用分支和提交；当前 Skill 指针未切换。\n"
        "- 未调用远端 API，尚未创建、合并或部署 GitHub PR。\n\n"
        "## 风险与注意事项\n\n"
        "- 这是待人工审阅的 Draft PR，必须通过 CI 并执行独立的 `/evolve publish` 二次确认。\n"
        "- 本阶段不会合并 main、部署 Gateway 或修改运行中的 Skill。"
    )


def create_draft_pr(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    repo_path: str | Path,
    actor: str,
    config: Any,
    run_git: _RUN = subprocess.run,
) -> DraftPRResult:
    """Prepare a Draft PR branch for an approved non-workspace Proposal.

    ``repo_path`` is intentionally explicit so callers cannot accidentally
    publish from an unrelated working directory.  The repository must be clean
    before the operation; only the candidate Skill file is staged.
    """

    if not actor.strip():
        raise ValueError("Draft PR actor is required")
    if not _enabled(config):
        return DraftPRResult("disabled", proposal_id, reason="Draft PR creation is disabled")
    repo = Path(repo_path).expanduser().resolve()
    row = _load_proposal(connection, proposal_id)
    if row is None:
        return DraftPRResult("not_found", proposal_id)
    if row[3] not in _ALLOWED_SOURCES or row[4] != "git_pr_proposal":
        return DraftPRResult("not_pr_proposal", proposal_id)
    if row[5] == "pr_created":
        branch = _branch_name(proposal_id)
        check = _run_git(run_git, repo, "show-ref", "--verify", f"refs/heads/{branch}")
        return DraftPRResult("idempotent", proposal_id, branch=branch, reason=None if check.returncode == 0 else "branch missing")
    if row[5] != "approved":
        return DraftPRResult("conflict", proposal_id, reason=f"status={row[5]}")
    if not row[6] or not row[7] or row[16] is None or row[17] != row[9]:
        return DraftPRResult("stale", proposal_id, reason="candidate revision is incomplete or hash mismatched")
    if row[15] and row[15] != row[6]:
        return DraftPRResult("stale", proposal_id, reason="current Skill revision changed")
    content = str(row[16])
    if _hash(content) != row[9]:
        return DraftPRResult("stale", proposal_id, reason="candidate content hash mismatch")
    try:
        _safe_component(str(row[2]), "Skill name")
    except ValueError as exc:
        return DraftPRResult("invalid", proposal_id, reason=str(exc))

    root_check = _run_git(run_git, repo, "rev-parse", "--show-toplevel")
    if root_check.returncode != 0 or Path(root_check.stdout.strip()).resolve() != repo:
        return DraftPRResult("invalid_repo", proposal_id, reason="repo_path is not a Git worktree")
    clean = _run_git(run_git, repo, "status", "--porcelain")
    if clean.returncode != 0:
        return DraftPRResult("invalid_repo", proposal_id, reason=_git_error(clean))
    if clean.stdout.strip():
        return DraftPRResult("dirty_repo", proposal_id, reason="Git worktree must be clean")

    path = repo / "skills" / str(row[2]) / "SKILL.md"
    if path.exists() and _hash(path.read_text(encoding="utf-8")) != row[8]:
        return DraftPRResult("stale", proposal_id, reason="repository baseline file hash mismatch")

    branch = _branch_name(proposal_id)
    existing = _run_git(run_git, repo, "show-ref", "--verify", f"refs/heads/{branch}")
    if existing.returncode == 0:
        return DraftPRResult("conflict", proposal_id, branch=branch, reason="dedicated branch already exists")
    switched = _run_git(run_git, repo, "switch", "-c", branch)
    if switched.returncode != 0:
        return DraftPRResult("failed", proposal_id, branch=branch, reason=_git_error(switched))

    _atomic_write(path, content)
    relative = path.relative_to(repo).as_posix()
    added = _run_git(run_git, repo, "add", "--", relative)
    if added.returncode != 0:
        return DraftPRResult("failed", proposal_id, branch=branch, path=str(path), reason=_git_error(added))
    title = f"功能（Skill进化）：更新 {row[2]}"
    committed = _run_git(run_git, repo, "commit", "-m", title)
    if committed.returncode != 0:
        return DraftPRResult("failed", proposal_id, branch=branch, path=str(path), reason=_git_error(committed))
    commit_check = _run_git(run_git, repo, "rev-parse", "HEAD")
    commit = commit_check.stdout.strip() if commit_check.returncode == 0 else "unknown"
    body = _make_body(row, branch=branch, commit=commit)
    try:
        ProposalRepository(connection).transition(
            proposal_id, expected_status="approved", new_status="creating_pr", expected_epoch=int(row[10])
        )
        ProposalRepository(connection).transition(
            proposal_id, expected_status="creating_pr", new_status="pr_created"
        )
        ProposalRepository(connection)._record_action(  # type: ignore[attr-defined]
            proposal_id=proposal_id, workspace=str(row[0]), action="create_draft_pr", actor_openid=actor,
            group_openid=None, idempotency_key=f"draft-pr:{proposal_id}",
            request_digest=_hash(proposal_id), result_status="pr_created",
            result={"branch": branch, "commit": commit, "title": title},
        )
    except (ProposalConflict, sqlite3.Error) as exc:
        return DraftPRResult("failed", proposal_id, branch=branch, commit=commit, title=title, body=body,
                             path=str(path), reason=f"Proposal transition failed: {exc}")
    return DraftPRResult("pr_created", proposal_id, branch=branch, commit=commit, title=title,
                         body=body, path=str(path))


__all__ = ["DraftPRResult", "create_draft_pr"]
