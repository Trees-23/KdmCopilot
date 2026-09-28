from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nanobot.memory.phase6_runtime import ensure_overlay_checkout


def _completed(args, *, cwd=None, **_kwargs):
    command = list(args)
    if command[:3] == ["gh", "repo", "clone"]:
        Path(command[4]).mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, "", "")
    if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
        return subprocess.CompletedProcess(command, 0, str(cwd) + "\n", "")
    if command[:3] == ["git", "remote", "get-url"]:
        return subprocess.CompletedProcess(command, 0, "git@github.com:Trees-23/KdmCopilot-skills-private.git\n", "")
    if command[:3] == ["git", "status", "--porcelain"]:
        return subprocess.CompletedProcess(command, 0, "", "")
    if command[:3] == ["git", "branch", "--show-current"]:
        return subprocess.CompletedProcess(command, 0, "main\n", "")
    raise AssertionError(command)


def test_overlay_checkout_is_created_and_validated(tmp_path: Path) -> None:
    checkout = ensure_overlay_checkout(
        "Trees-23/KdmCopilot-skills-private",
        tmp_path / "overlay",
        run=_completed,
    )
    assert checkout == (tmp_path / "overlay").resolve()


def test_overlay_checkout_rejects_dirty_existing_worktree(tmp_path: Path) -> None:
    checkout = tmp_path / "overlay"
    checkout.mkdir()

    def dirty(args, **kwargs):
        result = _completed(args, **kwargs)
        if list(args)[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(list(args), 0, " M skills/demo/SKILL.md\n", "")
        return result

    with pytest.raises(RuntimeError, match="dirty"):
        ensure_overlay_checkout("Trees-23/KdmCopilot-skills-private", checkout, run=dirty)
