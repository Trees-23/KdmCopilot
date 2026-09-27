from __future__ import annotations

import io
import json
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from nanobot.memory.overlay_deployer import OverlayGatewayDeployer, build_publish_callbacks
from nanobot.memory.overlay_release import OverlayReleaseError
from nanobot.memory.pr_publisher import create_draft_pr
from nanobot.memory.publish_gate import issue_publish_confirmation, publish_proposal
from tests.memory.test_pr_publisher import _config, _setup

SHA = "a" * 40


def _tar(*names: tuple[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in names:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def test_deployer_syncs_overlay_and_runs_rebuild(tmp_path):
    calls = []
    payload = _tar(("owner-repo/skills/demo/SKILL.md", b"# deployed\n"))

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(command, 0, payload, b"")
        return subprocess.CompletedProcess(command, 0, "Build reference: git-test123\n", "")

    deployer = OverlayGatewayDeployer(
        "Trees-23/KdmCopilot-skills-private",
        project_root=tmp_path / "project",
        workspace=tmp_path / "workspace",
        rebuild_script=tmp_path / "project" / "rebuild.sh",
        run=run,
    )
    result = deployer.deploy(SHA)
    assert result == f"git-test123 overlay={SHA} skills=1"
    assert (tmp_path / "workspace/skills/demo/SKILL.md").read_text() == "# deployed\n"
    assert calls[0][0] == ["gh", "api", f"repos/Trees-23/KdmCopilot-skills-private/tarball/{SHA}"]


def test_deployer_rejects_path_traversal(tmp_path):
    payload = _tar(("owner-repo/skills/../escape/SKILL.md", b"bad"))

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, payload, b"")

    deployer = OverlayGatewayDeployer(
        "Trees-23/KdmCopilot-skills-private",
        project_root=tmp_path,
        workspace=tmp_path / "workspace",
        rebuild_script=tmp_path / "rebuild.sh",
        run=run,
    )
    with pytest.raises(OverlayReleaseError, match="unsafe path"):
        deployer.deploy(SHA)


def test_deployer_requires_build_evidence(tmp_path):
    payload = _tar(("owner-repo/skills/demo/SKILL.md", b"# deployed\n"))

    def run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(command, 0, payload, b"")
        return subprocess.CompletedProcess(command, 0, "Gateway started\n", "")

    deployer = OverlayGatewayDeployer(
        "Trees-23/KdmCopilot-skills-private",
        project_root=tmp_path,
        workspace=tmp_path / "workspace",
        rebuild_script=tmp_path / "rebuild.sh",
        run=run,
    )
    with pytest.raises(OverlayReleaseError, match="build reference"):
        deployer.deploy(SHA)


def test_publish_gate_uses_overlay_merge_and_deploy_callbacks(tmp_path):
    repo, connection = _setup(tmp_path)
    proposal_id = "prop-shared-test"
    assert create_draft_pr(
        connection, proposal_id, repo_path=repo, actor="admin", config=_config()
    ).status == "pr_created"
    issue_publish_confirmation(connection, proposal_id, actor="admin", code="654321")
    calls: list[str] = []

    class Release:
        def ci_passed(self, value):
            calls.append(f"ci:{value}")
            return True

        def merge_branch(self, branch, commit):
            calls.append(f"merge:{branch}:{commit}")
            return SHA

    class Deployer:
        def deploy(self, merge_ref):
            calls.append(f"deploy:{merge_ref}")
            return "git-test overlay=" + merge_ref

    callbacks = build_publish_callbacks(Release(), Deployer())
    commit = json.loads(
        connection.execute(
            "SELECT result_json FROM proposal_actions WHERE proposal_id=? AND action='create_draft_pr'",
            (proposal_id,),
        ).fetchone()[0]
    )["commit"]
    result = publish_proposal(
        connection,
        proposal_id,
        actor="admin",
        code="654321",
        config=SimpleNamespace(
            enabled=True,
            kill_switch=False,
            evolution=SimpleNamespace(adoption_enabled=True, publish_enabled=True),
        ),
        ci_passed=callbacks.ci_passed(proposal_id),
        merge=callbacks.merge,
        deploy=callbacks.deploy,
    )
    assert result.status == "published"
    assert calls == [
        f"ci:{proposal_id}",
        f"merge:evolve/prop-shared-test:{commit}",
        f"deploy:{SHA}",
    ]
    connection.close()
