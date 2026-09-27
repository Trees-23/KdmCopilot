"""Read-only mapping between a Skill Overlay checkout and a Gateway workspace.

The first Overlay integration is intentionally inspection-only.  It verifies
the repository branch, safe Skill paths, content hashes and workspace drift;
writing or publishing remains an explicit M9 operation.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_SAFE_NAME = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
_RUN = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class OverlaySkill:
    skill_name: str
    overlay_path: str
    overlay_hash: str
    workspace_path: str
    workspace_hash: str | None
    status: str


@dataclass(frozen=True, slots=True)
class OverlayInspection:
    status: str
    repository: str
    branch: str | None
    skills: tuple[OverlaySkill, ...] = ()
    reason: str | None = None


def _digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _safe_name(value: str) -> bool:
    return bool(value) and value[0] != "." and set(value) <= _SAFE_NAME


def _run_git(run: _RUN, repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=repository, check=False, capture_output=True, text=True)


def inspect_overlay(
    repository: str | Path,
    workspace: str | Path,
    *,
    expected_branch: str = "main",
    run_git: _RUN = subprocess.run,
) -> OverlayInspection:
    """Inspect Overlay-to-workspace correspondence without changing either path."""

    repo = Path(repository).expanduser().resolve()
    target = Path(workspace).expanduser().resolve()
    if not repo.is_dir() or not target.is_dir():
        return OverlayInspection("invalid", str(repo), None, reason="repository/workspace must be directories")
    root = _run_git(run_git, repo, "rev-parse", "--show-toplevel")
    if root.returncode != 0 or Path(root.stdout.strip()).resolve() != repo:
        return OverlayInspection("invalid", str(repo), None, reason="repository is not a Git worktree")
    branch_result = _run_git(run_git, repo, "branch", "--show-current")
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    if branch != expected_branch:
        return OverlayInspection("branch_mismatch", str(repo), branch, reason=f"expected branch {expected_branch!r}")
    skills_root = repo / "skills"
    if not skills_root.exists():
        return OverlayInspection("empty", str(repo), branch)
    records: list[OverlaySkill] = []
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir() or skill_dir.is_symlink():
            continue
        name = skill_dir.name
        if not _safe_name(name):
            return OverlayInspection("invalid", str(repo), branch, reason=f"unsafe Skill name: {name!r}")
        path = skill_dir / "SKILL.md"
        if not path.is_file() or path.is_symlink():
            return OverlayInspection("invalid", str(repo), branch, reason=f"missing or linked SKILL.md: {name}")
        resolved = path.resolve()
        if repo != resolved and repo not in resolved.parents:
            return OverlayInspection("invalid", str(repo), branch, reason=f"Skill path escapes repository: {name}")
        content = path.read_text(encoding="utf-8")
        overlay_hash = _digest(content)
        workspace_path = target / "skills" / name / "SKILL.md"
        workspace_hash = _digest(workspace_path.read_text(encoding="utf-8")) if workspace_path.is_file() else None
        status = "in_sync" if workspace_hash == overlay_hash else ("missing_in_workspace" if workspace_hash is None else "drifted")
        records.append(OverlaySkill(name, str(path), overlay_hash, str(workspace_path), workspace_hash, status))
    overall = "in_sync" if records and all(item.status == "in_sync" for item in records) else "drifted"
    return OverlayInspection(overall, str(repo), branch, tuple(records))


__all__ = ["OverlayInspection", "OverlaySkill", "inspect_overlay"]
