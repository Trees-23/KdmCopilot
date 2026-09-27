from __future__ import annotations

import io
import subprocess
import tarfile

import pytest

from nanobot.memory.overlay_deployer import OverlayGatewayDeployer
from nanobot.memory.overlay_release import OverlayReleaseError

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
