"""Gateway-owned Phase 6 Overlay runtime adapters.

The scheduled reviewer may create a private Draft PR only when all three
pieces are configured: ``draftPrEnabled``, a private Overlay repository and a
dedicated clean checkout.  The checkout is created lazily on the first due
run, never from an Agent turn.  CI is polled separately and QQ is queued only
after the remote Draft PR is confirmed healthy.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from nanobot.memory.overlay_pipeline import OverlayProposalPipeline
from nanobot.memory.overlay_remote import GitHubOverlayClient

_RUN = Callable[..., subprocess.CompletedProcess[str]]


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
    return target


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
    checkout = configured_path or (Path(workspace).resolve().parent / "phase6-overlay")
    checkout = ensure_overlay_checkout(repository, checkout, base_branch=branch)
    client = GitHubOverlayClient(
        checkout,
        repository=repository,
        base_branch=branch,
        actor="phase6-overlay",
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

    return OverlayProposalPipeline(
        str(workspace),
        create_draft_pr=create_draft,
        notify_publish_candidate=notify,
        refresh_ci=refresh_ci,
    )


__all__ = ["build_overlay_pipeline", "ensure_overlay_checkout"]
