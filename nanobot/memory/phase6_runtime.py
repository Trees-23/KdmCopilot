"""Gateway-owned Phase 6 Overlay runtime adapters.

The scheduled reviewer may create a private Draft PR only when all three
pieces are configured: ``draftPrEnabled``, a private Overlay repository and a
dedicated clean checkout.  The checkout is created lazily on the first due
run, never from an Agent turn.  CI is polled separately and QQ is queued only
after the remote Draft PR is confirmed healthy.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from nanobot.memory.overlay_pipeline import OverlayProposalPipeline
from nanobot.memory.overlay_remote import GitHubOverlayClient

_RUN = Callable[..., subprocess.CompletedProcess[str]]
_AUTOMATION_GIT_NAME = "nanobot Skill Evolution"
_AUTOMATION_GIT_EMAIL = "nanobot-skill-evolution@users.noreply.github.com"


def _evolution(config: Any) -> Any:
    return getattr(config, "evolution", config)


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GH_PROMPT_DISABLED": "1"},
    )


def ensure_overlay_checkout(
    repository: str,
    checkout_path: str | Path,
    *,
    base_branch: str = "main",
    run: _RUN = subprocess.run,
) -> Path:
    """Create or validate one clean private Overlay checkout.

    Existing user changes are never overwritten.  A dirty or mismatched
    checkout fails closed and the scheduled run keeps its Proposal without
    attempting a PR handoff.
    """

    target = Path(checkout_path).expanduser().resolve()
    if target.exists() and not target.is_dir():
        raise RuntimeError("Overlay checkout path is not a directory")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not target.exists():
        cloned = run(
            ["gh", "repo", "clone", repository, str(target), "--", "--branch", base_branch],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
        if cloned.returncode != 0:
            detail = (cloned.stderr or cloned.stdout or "Overlay clone failed").strip()[:300]
            raise RuntimeError(detail)
    root = run(["git", "rev-parse", "--show-toplevel"], cwd=target, check=False,
               capture_output=True, text=True)
    if root.returncode != 0 or Path(root.stdout.strip()).resolve() != target:
        raise RuntimeError("Overlay checkout is not a Git worktree")
    remote = run(["git", "remote", "get-url", "origin"], cwd=target, check=False,
                 capture_output=True, text=True)
    if remote.returncode != 0:
        raise RuntimeError("Overlay checkout has no origin")
    status = run(["git", "status", "--porcelain"], cwd=target, check=False,
                 capture_output=True, text=True)
    if status.returncode != 0 or status.stdout.strip():
        raise RuntimeError("Overlay checkout is dirty")
    branch = run(["git", "branch", "--show-current"], cwd=target, check=False,
                 capture_output=True, text=True)
    if branch.returncode != 0 or branch.stdout.strip() != base_branch:
        raise RuntimeError(f"Overlay checkout must be on {base_branch}")
    for key, value in (("user.name", _AUTOMATION_GIT_NAME), ("user.email", _AUTOMATION_GIT_EMAIL)):
        configured = run(["git", "config", "--local", "--get", key], cwd=target, check=False,
                         capture_output=True, text=True)
        if configured.returncode != 0 or not configured.stdout.strip():
            saved = run(["git", "config", "--local", key, value], cwd=target, check=False,
                        capture_output=True, text=True)
            if saved.returncode != 0:
                detail = (saved.stderr or saved.stdout or "Overlay Git identity configuration failed").strip()[:300]
                raise RuntimeError(detail)
    return target


def _branch_name(proposal_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", proposal_id).strip("-.")
    return f"evolve/{clean[:80]}"


def _content_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def recover_abandoned_overlay_candidate(
    connection: Any,
    checkout_path: str | Path,
    *,
    workspace: str | Path,
    base_branch: str = "main",
    run: _RUN = _run,
) -> bool:
    """Remove only a verified system-generated, uncommitted failed candidate.

    A failed ``git commit`` may leave the reusable Overlay checkout on its
    dedicated proposal branch with a staged file.  This recovery refuses every
    other dirty shape: it requires a failed, Gate-passed Proposal, the exact
    generated branch/path, and a byte-for-byte candidate hash match.  It never
    uses ``git reset``/``clean`` and never touches a user-authored change.
    """
    checkout = Path(checkout_path).expanduser().resolve()
    if not checkout.is_dir():
        return False
    root = run(["git", "rev-parse", "--show-toplevel"], cwd=checkout, check=False,
               capture_output=True, text=True)
    if root.returncode != 0 or Path(root.stdout.strip()).resolve() != checkout:
        raise RuntimeError("Overlay checkout is not a Git worktree")
    status = run(["git", "status", "--porcelain"], cwd=checkout, check=False,
                 capture_output=True, text=True)
    if status.returncode != 0:
        raise RuntimeError("Overlay checkout status cannot be read")
    rows = [line for line in status.stdout.splitlines() if line.strip()]
    if not rows:
        return False
    if len(rows) != 1 or not rows[0].startswith("A  skills/"):
        raise RuntimeError("Overlay checkout has unrecognized changes; refusing automatic recovery")
    relative = rows[0][3:]
    branch = run(["git", "branch", "--show-current"], cwd=checkout, check=False,
                 capture_output=True, text=True)
    if branch.returncode != 0:
        raise RuntimeError("Overlay checkout branch cannot be read")
    candidate = connection.execute(
        "SELECT proposal_id,skill_name,candidate_hash FROM skill_proposals "
        "WHERE workspace=? AND target='git_pr_proposal' AND status='failed' AND gate_result='passed'",
        (str(Path(workspace).expanduser().resolve()),),
    ).fetchall()
    matching = []
    for proposal_id, skill_name, candidate_hash in candidate:
        expected = f"skills/{skill_name}/SKILL.md"
        path = checkout / expected
        if branch.stdout.strip() == _branch_name(str(proposal_id)) and relative == expected and path.is_file():
            if _content_hash(path.read_text(encoding="utf-8")) == str(candidate_hash):
                matching.append((str(proposal_id), path))
    if len(matching) != 1:
        raise RuntimeError("Overlay dirty file is not a verified failed system candidate")
    _proposal_id, path = matching[0]
    unstaged = run(["git", "rm", "--cached", "--", relative], cwd=checkout, check=False,
                   capture_output=True, text=True)
    if unstaged.returncode != 0:
        raise RuntimeError((unstaged.stderr or unstaged.stdout or "Overlay index recovery failed").strip()[:300])
    path.unlink()
    switched = run(["git", "switch", base_branch], cwd=checkout, check=False,
                   capture_output=True, text=True)
    if switched.returncode != 0:
        raise RuntimeError((switched.stderr or switched.stdout or "Overlay base branch recovery failed").strip()[:300])
    return True


def build_overlay_pipeline(
    connection: Any,
    workspace: str | Path,
    config: Any,
    *,
    notifier: Any | None = None,
) -> OverlayProposalPipeline | None:
    """Build the protected Draft PR/CI/QQ adapter for one review cycle."""

    evolution = _evolution(config)
    if not bool(getattr(config, "enabled", False)) or bool(getattr(config, "kill_switch", False)):
        return None
    if not bool(getattr(evolution, "draft_pr_enabled", False)):
        return None
    repository = str(getattr(evolution, "overlay_repository", "") or "").strip()
    if not repository:
        return None
    branch = str(getattr(evolution, "overlay_base_branch", "main") or "main")
    configured_path = getattr(evolution, "overlay_checkout_path", None)
    checkout = configured_path or (Path(workspace).resolve().parent / "skill-evolution-overlay")
    recover_abandoned_overlay_candidate(connection, checkout, workspace=workspace, base_branch=branch)
    checkout = ensure_overlay_checkout(repository, checkout, base_branch=branch)
    client = GitHubOverlayClient(
        checkout,
        repository=repository,
        base_branch=branch,
        actor="skill-evolution-overlay",
    )
    groups = tuple(str(item) for item in (getattr(evolution, "notification_groups", ()) or ()) if str(item))

    def create_draft(proposal_id: str) -> Any:
        return client.create_draft_pr(connection, proposal_id, config)

    def refresh_ci(proposal_id: str) -> Any:
        return client.inspect_ci(connection, proposal_id)

    def notify(proposal_id: str) -> Any:
        if notifier is None or not groups:
            return None
        return notifier.enqueue(proposal_id, group_openid=groups[0])

    def notify_failure(proposal_id: str, reason: str) -> Any:
        if notifier is None:
            return None
        return notifier.enqueue_pipeline_failure(proposal_id, reason)

    return OverlayProposalPipeline(
        str(workspace),
        create_draft_pr=create_draft,
        notify_publish_candidate=notify,
        notify_failure=notify_failure,
        refresh_ci=refresh_ci,
    )


__all__ = ["build_overlay_pipeline", "ensure_overlay_checkout", "recover_abandoned_overlay_candidate"]
