"""Controlled workspace Skill adoption with filesystem and SQLite CAS."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from nanobot.memory.continuous import phase6_active

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    status: str
    proposal_id: str
    revision_id: str | None = None
    path: str | None = None
    reason: str | None = None


def _iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _evolution(config: Any) -> Any:
    return getattr(config, "evolution", config)


def _enabled(config: Any) -> bool:
    return phase6_active(config) and bool(getattr(_evolution(config), "adoption_enabled", False))


def _allowed_skill(config: Any, skill_name: str) -> bool:
    allowlist = getattr(_evolution(config), "workspace_skill_allowlist", ()) or ()
    return skill_name in {str(item) for item in allowlist}


def _skill_path(workspace: str | Path, skill_name: str) -> Path:
    if not _SAFE_NAME.fullmatch(skill_name):
        raise ValueError("unsafe workspace Skill name")
    root = Path(workspace).expanduser().resolve()
    path = (root / "skills" / skill_name / "SKILL.md").resolve()
    if root != path and root not in path.parents:
        raise ValueError("Skill path escapes workspace")
    return path


def _atomic_write(path: Path, content: str) -> tuple[bool, bytes | None, int | None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old_bytes = path.read_bytes() if path.exists() else None
    old_mode = path.stat().st_mode & 0o777 if path.exists() else None
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    if old_mode is not None:
        temporary.chmod(old_mode)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return old_bytes is not None, old_bytes, old_mode


def _restore(path: Path, old_bytes: bytes | None, old_mode: int | None) -> None:
    if old_bytes is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.write_bytes(old_bytes)
    if old_mode is not None:
        path.chmod(old_mode)


def adopt_workspace_proposal(
    connection: sqlite3.Connection,
    workspace: str | Path,
    proposal_id: str,
    *,
    actor: str,
    config: Any,
) -> AdoptionResult:
    """Atomically install an approved workspace Proposal's candidate revision."""
    if not actor.strip():
        raise ValueError("adoption actor is required")
    if not _enabled(config):
        return AdoptionResult("disabled", proposal_id, reason="workspace adoption is disabled")
    row = connection.execute(
        "SELECT p.workspace,p.skill_id,p.skill_name,p.source_kind,p.target,p.status,"
        "p.baseline_revision_id,p.candidate_revision_id,p.baseline_hash,p.candidate_hash,"
        "p.version_epoch,s.current_revision_id,r.content,r.content_hash,r.skill_version "
        "FROM skill_proposals p LEFT JOIN skills s ON s.skill_id=p.skill_id "
        "LEFT JOIN skill_revisions r ON r.revision_id=p.candidate_revision_id "
        "WHERE p.proposal_id=?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return AdoptionResult("not_found", proposal_id)
    (proposal_workspace, skill_id, skill_name, source_kind, target, status, baseline_revision,
     candidate_revision, baseline_hash, candidate_hash, epoch, current_revision, content,
     revision_hash, skill_version) = row
    if str(proposal_workspace) != str(Path(workspace).expanduser().resolve()):
        return AdoptionResult("workspace_mismatch", proposal_id)
    if source_kind != "workspace" or target != "workspace_adopt_proposal":
        return AdoptionResult("not_workspace_proposal", proposal_id)
    if not _allowed_skill(config, str(skill_name)):
        return AdoptionResult("not_allowed", proposal_id, reason="Skill is not in workspace adoption allowlist")
    if status != "approved":
        return AdoptionResult("conflict", proposal_id, reason=f"status={status}")
    if not skill_id or not candidate_revision or content is None:
        return AdoptionResult("invalid", proposal_id, reason="candidate revision is incomplete")
    if revision_hash != candidate_hash or _digest(content) != candidate_hash:
        return AdoptionResult("stale", proposal_id, reason="candidate hash mismatch")
    if current_revision != baseline_revision:
        return AdoptionResult("stale", proposal_id, reason="current Skill revision changed")
    path = _skill_path(workspace, skill_name)
    if path.exists() and _digest(path.read_text(encoding="utf-8")) != baseline_hash:
        return AdoptionResult("stale", proposal_id, reason="workspace file hash changed")

    old_bytes: bytes | None = None
    old_mode: int | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT status,version_epoch FROM skill_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        pointer = connection.execute("SELECT current_revision_id FROM skills WHERE skill_id=?", (skill_id,)).fetchone()
        if current is None or current[0] != "approved" or int(current[1]) != int(epoch) or pointer is None or pointer[0] != baseline_revision:
            connection.rollback()
            return AdoptionResult("conflict", proposal_id, reason="Proposal or Skill changed")
        _had_file, old_bytes, old_mode = _atomic_write(path, content)
        timestamp = _iso()
        connection.execute(
            "UPDATE skills SET current_revision_id=?,current_version=?,updated_at=? "
            "WHERE skill_id=? AND current_revision_id=?",
            (candidate_revision, skill_version, timestamp, skill_id, baseline_revision),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise RuntimeError("Skill pointer CAS failed")
        connection.execute(
            "UPDATE skill_revisions SET status='active' WHERE revision_id=?", (candidate_revision,)
        )
        connection.execute(
            "UPDATE skill_proposals SET status='adopted',version_epoch=version_epoch+1,updated_at=? "
            "WHERE proposal_id=? AND status='approved' AND version_epoch=?",
            (timestamp, proposal_id, epoch),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise RuntimeError("Proposal CAS failed")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if old_bytes is not None or path.exists():
            _restore(path, old_bytes, old_mode)
        return AdoptionResult("failed", proposal_id, reason=str(exc)[:300])
    return AdoptionResult("adopted", proposal_id, candidate_revision, str(path))


def rollback_workspace_proposal(
    connection: sqlite3.Connection,
    workspace: str | Path,
    proposal_id: str,
    *,
    actor: str,
    code: str,
    config: Any,
) -> AdoptionResult:
    """Restore the baseline revision after an explicit administrator confirmation."""
    if not actor.strip() or not code.strip():
        raise ValueError("rollback actor and confirmation code are required")
    if not _enabled(config):
        return AdoptionResult("disabled", proposal_id, reason="workspace adoption is disabled")
    row = connection.execute(
        "SELECT p.workspace,p.skill_id,p.skill_name,p.status,p.baseline_revision_id,p.candidate_revision_id,"
        "s.current_revision_id,p.baseline_hash,p.confirmation_code_hash,p.confirmation_expires_at,p.version_epoch "
        "FROM skill_proposals p JOIN skills s ON s.skill_id=p.skill_id WHERE p.proposal_id=?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return AdoptionResult("not_found", proposal_id)
    (proposal_workspace, skill_id, skill_name, status, baseline_revision, candidate_revision,
     current_revision, baseline_hash, code_hash, expires, epoch) = row
    if str(proposal_workspace) != str(Path(workspace).expanduser().resolve()):
        return AdoptionResult("workspace_mismatch", proposal_id)
    if status != "adopted" or current_revision != candidate_revision:
        return AdoptionResult("conflict", proposal_id, reason="Proposal is not an adopted current revision")
    if not code_hash or not hmac.compare_digest(str(code_hash), "sha256:" + hashlib.sha256(code.encode()).hexdigest()):
        return AdoptionResult("invalid_code", proposal_id)
    if not expires or str(expires) <= _iso():
        return AdoptionResult("expired", proposal_id)
    revision = connection.execute(
        "SELECT content,content_hash,skill_version FROM skill_revisions WHERE revision_id=?", (baseline_revision,)
    ).fetchone()
    if revision is None or revision[1] != baseline_hash:
        return AdoptionResult("stale", proposal_id, reason="baseline revision mismatch")
    path = _skill_path(workspace, skill_name)
    old_bytes = path.read_bytes() if path.exists() else None
    old_mode = path.stat().st_mode & 0o777 if path.exists() else None
    try:
        connection.execute("BEGIN IMMEDIATE")
        _atomic_write(path, revision[0])
        timestamp = _iso()
        connection.execute(
            "UPDATE skills SET current_revision_id=?,current_version=?,updated_at=? WHERE skill_id=? AND current_revision_id=?",
            (baseline_revision, revision[2], timestamp, skill_id, candidate_revision),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise RuntimeError("rollback Skill pointer CAS failed")
        connection.execute(
            "UPDATE skill_proposals SET status='rolled_back',version_epoch=version_epoch+1,updated_at=? "
            "WHERE proposal_id=? AND status='adopted' AND version_epoch=?",
            (timestamp, proposal_id, epoch),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise RuntimeError("rollback Proposal CAS failed")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        _restore(path, old_bytes, old_mode)
        return AdoptionResult("failed", proposal_id, reason=str(exc)[:300])
    return AdoptionResult("rolled_back", proposal_id, baseline_revision, str(path))


__all__ = ["AdoptionResult", "adopt_workspace_proposal", "rollback_workspace_proposal"]
