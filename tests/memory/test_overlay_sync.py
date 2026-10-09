from __future__ import annotations

import subprocess

from nanobot.memory.overlay_sync import inspect_overlay


def _git(path, *args: str):
    return subprocess.run(["git", *args], cwd=path, check=False, capture_output=True, text=True)


def _setup(tmp_path):
    repo = tmp_path / "overlay"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    workspace.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Overlay Test")
    _git(repo, "config", "user.email", "overlay@example.invalid")
    skill = repo / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    _git(repo, "add", "--", "skills/demo/SKILL.md")
    _git(repo, "commit", "-m", "测试：初始化Overlay Skill")
    target = workspace / "skills" / "demo"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    return repo, workspace


def test_overlay_inspection_reports_hash_mapping_and_drift(tmp_path):
    repo, workspace = _setup(tmp_path)
    inspected = inspect_overlay(repo, workspace)
    assert inspected.status == "in_sync"
    assert inspected.branch == "main"
    assert inspected.skills[0].status == "in_sync"
    assert inspected.skills[0].overlay_hash == inspected.skills[0].workspace_hash

    (workspace / "skills/demo/SKILL.md").write_text("# changed\n", encoding="utf-8")
    drifted = inspect_overlay(repo, workspace)
    assert drifted.status == "drifted"
    assert drifted.skills[0].status == "drifted"


def test_overlay_inspection_never_writes_and_rejects_wrong_branch(tmp_path):
    repo, workspace = _setup(tmp_path)
    before = (workspace / "skills/demo/SKILL.md").read_text(encoding="utf-8")
    _git(repo, "switch", "-c", "candidate")
    result = inspect_overlay(repo, workspace)
    assert result.status == "branch_mismatch"
    assert (workspace / "skills/demo/SKILL.md").read_text(encoding="utf-8") == before


def test_overlay_inspection_reports_missing_workspace_skill(tmp_path):
    repo, workspace = _setup(tmp_path)
    (workspace / "skills/demo/SKILL.md").unlink()
    result = inspect_overlay(repo, workspace)
    assert result.status == "drifted"
    assert result.skills[0].status == "missing_in_workspace"
