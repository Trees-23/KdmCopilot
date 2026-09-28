"""Controlled hot deployment of a merged personal Skill Overlay.

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
    """Sync only safe ``skills/*/SKILL.md`` files into the live workspace.

    ``SkillsLoader`` reads workspace Skill files while building each new Agent
    context.  Replacing the files atomically is therefore sufficient to make
    an Overlay publish visible to the next turn; replacing the Gateway
    container would unnecessarily disconnect WebUI and QQ clients.

    ``project_root`` and ``rebuild_script`` remain accepted for compatibility
    with the original M9 wiring, but are intentionally unused.  They can be
    removed once all external release workers have migrated to hot loading.
    """

    def __init__(
        self,
        repository: str,
        *,
        project_root: str | Path,
        workspace: str | Path,
        rebuild_script: str | Path | None = None,
        run: _RUN = subprocess.run,
    ) -> None:
        self.repository = repository
        self.project_root = Path(project_root).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve()
        self.rebuild_script = (
            Path(rebuild_script).expanduser().resolve() if rebuild_script is not None else None
        )
        self._run = run

    def _download_tarball(self, merge_ref: str) -> bytes:
        if not _SHA.fullmatch(merge_ref):
            raise OverlayReleaseError("merge reference must be a full commit SHA")
        result = self._run(
            ["gh", "api", f"repos/{self.repository}/tarball/{merge_ref}"],
            check=False,
            capture_output=True,
            text=False,
            env={
                **os.environ,
                "GH_PROMPT_DISABLED": "1",
                **(
                    {"GH_TOKEN": os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"]}
                    if os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
                    and not os.environ.get("GH_TOKEN")
                    else {}
                ),
            },
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
        # Do not restart or rebuild the Gateway here.  The workspace is a
        # bind-mounted runtime directory, and SkillsLoader performs a fresh
        # directory/file read for every new Context.  The writes above use a
        # temporary file + fsync + atomic rename, so a running turn keeps its
        # existing prompt while the next turn sees the published Skill.
        return f"hot-reload overlay={merge_ref} skills={count}"


def build_publish_callbacks(release: object, deployer: OverlayGatewayDeployer):
    """Build explicit M9 callbacks without giving the Agent arbitrary shell access."""
    from nanobot.memory.evolution_commands import PublishCallbacks

    return PublishCallbacks(
        ci_passed=release.ci_passed,  # type: ignore[attr-defined]
        merge=release.merge_branch,  # type: ignore[attr-defined]
        deploy=deployer.deploy,
    )


__all__ = ["DeploymentResult", "OverlayGatewayDeployer", "build_publish_callbacks"]
