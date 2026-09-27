"""Controlled deployment of a merged personal Skill Overlay.

This module is deliberately callback-oriented.  It never runs as part of a
normal Agent turn: a caller must inject it into the M9 publish gate after the
QQ second confirmation and Overlay CI checks have passed.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nanobot.memory.overlay_release import OverlayReleaseError

_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN = Callable[..., subprocess.CompletedProcess[bytes | str]]


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    status: str
    merge_ref: str
    build_ref: str | None = None
    reason: str | None = None


class OverlayGatewayDeployer:
    """Sync only safe ``skills/*/SKILL.md`` files and rebuild one Gateway."""

    def __init__(
        self,
        repository: str,
        *,
        project_root: str | Path,
        workspace: str | Path,
        rebuild_script: str | Path,
        run: _RUN = subprocess.run,
    ) -> None:
        self.repository = repository
        self.project_root = Path(project_root).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve()
        self.rebuild_script = Path(rebuild_script).expanduser().resolve()
        self._run = run

    def _download_tarball(self, merge_ref: str) -> bytes:
        if not _SHA.fullmatch(merge_ref):
            raise OverlayReleaseError("merge reference must be a full commit SHA")
        result = self._run(
            ["gh", "api", f"repos/{self.repository}/tarball/{merge_ref}"],
            check=False,
            capture_output=True,
            text=False,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
        if result.returncode != 0:
            detail = result.stderr or result.stdout or b"GitHub tarball request failed"
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            raise OverlayReleaseError(str(detail)[:300])
        payload = result.stdout
        if isinstance(payload, str):
            payload = payload.encode()
        return payload

    @staticmethod
    def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
        safe: list[tarfile.TarInfo] = []
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if not parts or ".." in parts or Path(member.name).is_absolute():
                raise OverlayReleaseError("Overlay tarball contains an unsafe path")
            if len(parts) >= 3 and parts[1] == "skills" and parts[-1] == "SKILL.md":
                if member.isfile():
                    safe.append(member)
        return safe

    def _sync_skills(self, payload: bytes) -> int:
        self.workspace.mkdir(mode=0o755, parents=True, exist_ok=True)
        target_root = self.workspace / "skills"
        target_root.mkdir(mode=0o755, parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = self._safe_members(archive)
            if not members:
                raise OverlayReleaseError("Overlay merge contains no skills/*/SKILL.md")
            count = 0
            for member in members:
                relative = Path(*Path(member.name).parts[2:])
                if len(relative.parts) != 2 or relative.parts[-1] != "SKILL.md":
                    raise OverlayReleaseError("Overlay Skill path must be skills/<name>/SKILL.md")
                destination = (target_root / relative).resolve()
                if target_root.resolve() not in destination.parents:
                    raise OverlayReleaseError("Overlay Skill path escapes workspace")
                destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise OverlayReleaseError("Overlay Skill file cannot be read")
                data = source.read()
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=destination.parent, prefix=".skill-", delete=False
                ) as temporary:
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = Path(temporary.name)
                os.replace(temporary_path, destination)
                count += 1
        return count

    def deploy(self, merge_ref: str) -> str:
        payload = self._download_tarball(merge_ref)
        count = self._sync_skills(payload)
        result = self._run(
            [str(self.rebuild_script)],
            cwd=self.project_root,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "NANOBOT_OVERLAY_COMMIT": merge_ref},
        )
        if result.returncode != 0:
            detail = result.stderr or result.stdout or "Gateway rebuild failed"
            raise OverlayReleaseError(str(detail)[-500:])
        output = result.stdout or ""
        match = re.search(r"Build reference:\s*(\S+)", output)
        if not match:
            raise OverlayReleaseError("Gateway rebuild did not report a build reference")
        return f"{match.group(1)} overlay={merge_ref} skills={count}"


def build_publish_callbacks(release: object, deployer: OverlayGatewayDeployer):
    """Build explicit M9 callbacks without giving the Agent arbitrary shell access."""
    from nanobot.memory.evolution_commands import PublishCallbacks

    return PublishCallbacks(
        ci_passed=release.ci_passed,  # type: ignore[attr-defined]
        merge=release.merge_branch,  # type: ignore[attr-defined]
        deploy=deployer.deploy,
    )


__all__ = ["DeploymentResult", "OverlayGatewayDeployer", "build_publish_callbacks"]
