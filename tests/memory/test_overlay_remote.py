from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

from nanobot.memory.overlay_remote import GitHubOverlayClient
from tests.memory.test_pr_publisher import _git, _setup


def _config():
    return SimpleNamespace(
        enabled=True,
        kill_switch=False,
        evolution=SimpleNamespace(draft_pr_enabled=True, adoption_enabled=False, publish_enabled=False),
    )


def _gh_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr="")


def test_private_overlay_client_creates_only_draft_pr_and_records_remote_metadata(tmp_path):
    repo, connection = _setup(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:Trees-23/KdmCopilot-skills-private.git")

    def run(command, **kwargs):
        if command[0] == "gh" and command[1:3] == ["pr", "list"]:
            return _gh_result("[]")
        if command[0] == "gh" and command[1:3] == ["pr", "create"]:
            return _gh_result("https://github.com/Trees-23/KdmCopilot-skills-private/pull/42\n")
        if command[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.run(command, **kwargs)

    client = GitHubOverlayClient(
        repo,
        repository="Trees-23/KdmCopilot-skills-private",
        run=run,
    )
    result = client.create_draft_pr(connection, "prop-shared-test", _config())

    assert result.status == "pr_created"
    assert result.url and result.url.endswith("/pull/42")
    action = connection.execute(
        "SELECT result_json FROM proposal_actions WHERE proposal_id=? AND action='remote_draft_pr'",
        ("prop-shared-test",),
    ).fetchone()
    assert action is not None
    metadata = json.loads(action[0])
    assert metadata["repository"] == "Trees-23/KdmCopilot-skills-private"
    assert metadata["base"] == "main"
    assert metadata["is_draft"] is True
    assert connection.execute(
        "SELECT status FROM skill_proposals WHERE proposal_id=?", ("prop-shared-test",)
    ).fetchone()[0] == "pr_created"
    connection.close()


def test_private_overlay_client_requires_matching_origin(tmp_path):
    repo, connection = _setup(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:someone/other.git")
    client = GitHubOverlayClient(repo, repository="Trees-23/KdmCopilot-skills-private")

    result = client.create_draft_pr(connection, "prop-shared-test", _config())

    assert result.status == "failed"
    assert "origin" in (result.reason or "")
    assert connection.execute(
        "SELECT status FROM skill_proposals WHERE proposal_id=?", ("prop-shared-test",)
    ).fetchone()[0] == "approved"
    connection.close()


def test_private_overlay_client_maps_project_token_to_gh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "test-project-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return _gh_result("")

    client = GitHubOverlayClient(
        tmp_path,
        repository="Trees-23/KdmCopilot-skills-private",
        run=run,
    )
    client._gh("auth", "status")

    assert captured["command"] == ["gh", "auth", "status"]
    assert captured["env"]["GH_TOKEN"] == "test-project-token"
    assert captured["env"]["GH_PROMPT_DISABLED"] == "1"
    assert os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"] == "test-project-token"


def test_private_overlay_client_scopes_proxy_and_https_git_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "test-project-token")
    monkeypatch.setenv("NANOBOT_GITHUB_PROXY_URL", "http://proxy.example:7890")
    client = GitHubOverlayClient(tmp_path, repository="Trees-23/KdmCopilot-skills-private")

    env = client._command_env()

    assert env["HTTPS_PROXY"] == "http://proxy.example:7890"
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "url.https://github.com/.insteadOf"
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_1"].startswith("AUTHORIZATION: basic ")


def test_private_overlay_client_ci_projection_only_notifies_after_success(tmp_path):
    repo, connection = _setup(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:Trees-23/KdmCopilot-skills-private.git")

    def run(command, **kwargs):
        if command[0] == "gh" and command[1:3] == ["pr", "list"]:
            return _gh_result("[]")
        if command[0] == "gh" and command[1:3] == ["pr", "create"]:
            return _gh_result("https://github.com/Trees-23/KdmCopilot-skills-private/pull/43\n")
        if command[0] == "gh" and command[1:3] == ["pr", "view"]:
            return _gh_result(json.dumps({
                "number": 43,
                "url": "https://github.com/Trees-23/KdmCopilot-skills-private/pull/43",
                "isDraft": True,
                "baseRefName": "main",
                "headRefName": "evolve/prop-shared-test",
                "headRefOid": "a" * 40,
                "state": "OPEN",
            }))
        if command[0] == "gh" and command[1:2] == ["api"]:
            return _gh_result(json.dumps({
                "workflow_runs": [{"status": "completed", "conclusion": "success"}],
            }))
        if command[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.run(command, **kwargs)

    client = GitHubOverlayClient(repo, repository="Trees-23/KdmCopilot-skills-private", run=run)
    created = client.create_draft_pr(connection, "prop-shared-test", _config())
    assert created.status == "pr_created"
    status = client.inspect_ci(connection, "prop-shared-test")
    assert status.status == "ci_passed"
    assert status.ci_passed is True
    connection.close()
