"""Second-confirmation gate for publishing an already-created Draft PR.

The module owns only the durable safety gate and callback orchestration.  A
real GitHub merge or deployment client must be injected explicitly by a later
deployment integration; there is no network or shell publishing fallback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from nanobot.memory.continuous import phase6_active
from nanobot.memory.proposal_repository import ProposalConflict, ProposalRepository


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _iso(value: datetime | None = None) -> str:
    return _now(value).isoformat(timespec="seconds")


def _hash_code(code: str) -> str:
    return "sha256:" + hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PublishChallenge:
    status: str
    proposal_id: str
    code: str | None = None
    expires_at: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    status: str
    proposal_id: str
    reason: str | None = None
    merge_ref: str | None = None
    deploy_ref: str | None = None


def _evolution(config: Any) -> Any:
    return getattr(config, "evolution", config)


def _active(config: Any) -> bool:
    return phase6_active(config) and bool(getattr(_evolution(config), "publish_enabled", False))


def issue_publish_confirmation(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    actor: str,
    ttl_minutes: int = 15,
    code: str | None = None,
    now: datetime | None = None,
) -> PublishChallenge:
    """Issue a one-time second confirmation code for a ``pr_created`` Proposal."""

    if not actor.strip():
        raise ValueError("publish confirmation actor is required")
    if ttl_minutes < 1:
        raise ValueError("publish confirmation TTL must be positive")
    record = ProposalRepository(connection).get(proposal_id)
    if record is None:
        return PublishChallenge("not_found", proposal_id)
    if record.status != "pr_created":
        return PublishChallenge("conflict", proposal_id, reason=f"status={record.status}")
    plain = code or f"{secrets.randbelow(1_000_000):06d}"
    if not plain.isdigit() or not 4 <= len(plain) <= 8:
        raise ValueError("publish confirmation code must be 4-8 digits")
    expires = _iso(_now(now) + timedelta(minutes=ttl_minutes))
    result = ProposalRepository(connection)._record_action(  # type: ignore[attr-defined]
        proposal_id=proposal_id,
        workspace=record.workspace,
        action="publish_challenge",
        actor_openid=actor,
        group_openid=None,
        idempotency_key=f"publish-challenge:{proposal_id}",
        request_digest=_hash_code(proposal_id),
        result_status="issued",
        result={"code_hash": _hash_code(plain), "expires_at": expires, "actor": actor},
        now=now,
    )
    if result.result.get("code_hash") != _hash_code(plain):
        # A replayed challenge never returns the old plaintext code.
        return PublishChallenge("idempotent", proposal_id, expires_at=result.result.get("expires_at"))
    return PublishChallenge("issued", proposal_id, code=plain, expires_at=expires)


def _proposal_action(connection: sqlite3.Connection, proposal_id: str, action: str) -> Mapping[str, Any] | None:
    row = connection.execute(
        "SELECT result_json FROM proposal_actions WHERE proposal_id=? AND action=? "
        "ORDER BY created_at DESC LIMIT 1",
        (proposal_id, action),
    ).fetchone()
    return json.loads(row[0]) if row else None


def publish_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    actor: str,
    code: str,
    config: Any,
    ci_passed: bool,
    merge: Callable[[str, str], str] | None = None,
    deploy: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> PublishResult:
    """Run the second-confirmation gate and injected merge/deploy callbacks.

    ``merge`` receives ``(branch, commit)`` and ``deploy`` receives the merge
    reference.  Both callbacks are mandatory, preventing accidental network
    or shell publication when the integration is not explicitly configured.
    """

    if not actor.strip() or not code.strip():
        raise ValueError("publish actor and confirmation code are required")
    if bool(getattr(config, "kill_switch", False)):
        return PublishResult("killed", proposal_id, "kill switch is active")
    if not _active(config):
        return PublishResult("disabled", proposal_id, "publishing is disabled")
    if not ci_passed:
        return PublishResult("ci_failed", proposal_id, "CI has not passed")
    if merge is None or deploy is None:
        return PublishResult("not_configured", proposal_id, "merge and deploy callbacks are required")
    repo = ProposalRepository(connection)
    record = repo.get(proposal_id)
    if record is None:
        return PublishResult("not_found", proposal_id)
    if record.status != "pr_created":
        return PublishResult("conflict", proposal_id, f"status={record.status}")
    challenge = _proposal_action(connection, proposal_id, "publish_challenge")
    if not challenge or not hmac.compare_digest(str(challenge.get("code_hash", "")), _hash_code(code)):
        return PublishResult("invalid_code", proposal_id)
    if not challenge.get("expires_at") or str(challenge["expires_at"]) <= _iso(now):
        return PublishResult("expired", proposal_id)
    draft = _proposal_action(connection, proposal_id, "create_draft_pr") or {}
    branch, commit = str(draft.get("branch", "")), str(draft.get("commit", ""))
    if not branch or not commit:
        return PublishResult("invalid", proposal_id, "Draft PR metadata is incomplete")
    try:
        repo.transition(proposal_id, expected_status="pr_created", new_status="publish_approved",
                        expected_epoch=record.version_epoch)
        repo.transition(proposal_id, expected_status="publish_approved", new_status="publishing")
        if bool(getattr(config, "kill_switch", False)):
            raise RuntimeError("kill switch activated before merge")
        merge_ref = merge(branch, commit)
        if bool(getattr(config, "kill_switch", False)):
            raise RuntimeError("kill switch activated before deploy")
        deploy_ref = deploy(merge_ref)
        repo.transition(proposal_id, expected_status="publishing", new_status="published")
    except (ProposalConflict, sqlite3.Error, RuntimeError, OSError) as exc:
        try:
            current = repo.get(proposal_id)
            if current and current.status in {"publish_approved", "publishing"}:
                repo.transition(proposal_id, expected_status=current.status, new_status="failed", reason=str(exc))
        except (ProposalConflict, sqlite3.Error):
            pass
        return PublishResult("failed", proposal_id, str(exc)[:300])
    return PublishResult("published", proposal_id, merge_ref=merge_ref, deploy_ref=deploy_ref)


__all__ = ["PublishChallenge", "PublishResult", "issue_publish_confirmation", "publish_proposal"]
