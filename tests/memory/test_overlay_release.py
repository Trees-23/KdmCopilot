from __future__ import annotations

import json
import subprocess

import pytest

from nanobot.memory.overlay_release import GitHubOverlayRelease, OverlayReleaseError


def _payload(*, repository="Trees-23/KdmCopilot-skills-private", draft=True, sha="abc"):
    return {
        "number": 1,
        "url": "https://github.com/Trees-23/KdmCopilot-skills-private/pull/1",
        "state": "OPEN",
        "isDraft": draft,
        "baseRepository": {"nameWithOwner": repository},
        "baseRefName": "main",
        "headRefName": "evolve/prop-test",
        "headRefOid": sha,
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    }


def test_overlay_client_verifies_ci_and_draft_merge():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["pr", "list"]:
            return subprocess.CompletedProcess(command, 0, json.dumps([_payload()]), "")
        return subprocess.CompletedProcess(command, 0, "merged", "")

    client = GitHubOverlayRelease("Trees-23/KdmCopilot-skills-private", run=run)
    assert client.ci_passed("prop-test") is True
    assert client.merge("prop-test", "abc") == "abc"
    assert any(command[1:3] == ["pr", "ready"] for command in calls)
    assert any(command[1:3] == ["pr", "merge"] for command in calls)


def test_overlay_client_rejects_wrong_repository_and_head():
    def run(command, **kwargs):
        payload = _payload(repository="Trees-23/KdmCopilot")
        return subprocess.CompletedProcess(command, 0, json.dumps([payload]), "")

    client = GitHubOverlayRelease("Trees-23/KdmCopilot-skills-private", run=run)
    with pytest.raises(OverlayReleaseError, match="private Overlay"):
        client.ci_passed("prop-test")


def test_overlay_client_requires_all_checks_to_pass():
    def run(command, **kwargs):
        payload = _payload(draft=False)
        payload["statusCheckRollup"] = [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        return subprocess.CompletedProcess(command, 0, json.dumps([payload]), "")

    client = GitHubOverlayRelease("Trees-23/KdmCopilot-skills-private", run=run)
    assert client.ci_passed("prop-test") is False
    with pytest.raises(OverlayReleaseError, match="CI"):
        client.merge("prop-test", "abc")


def test_overlay_client_adapts_publish_branch_contract():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps([_payload(draft=False)]), "")

    client = GitHubOverlayRelease("Trees-23/KdmCopilot-skills-private", run=run)
    assert client.merge_branch("evolve/prop-test", "abc") == "abc"
    with pytest.raises(OverlayReleaseError, match="canonical"):
        client.merge_branch("evolve/not the proposal", "abc")
