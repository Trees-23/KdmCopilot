"""Durable Proposal state, approval CAS, and notification outbox primitives.

This module deliberately contains no QQ or GitHub client code.  It provides a
small transaction boundary that later adapters (M3+) can use without allowing
an LLM turn to mutate a Proposal directly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import uuid4

PROPOSAL_STATUSES = frozenset({
    "draft", "evaluating", "rejected_by_gate", "insufficient_evidence", "stale",
    "eligible_for_confirmation", "notified", "approved", "adopting", "adopted",
    "creating_pr", "pr_created", "publish_approved", "publishing", "published",
    "rejected_by_admin", "expired", "failed", "rolled_back",
})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"evaluating", "rejected_by_gate", "insufficient_evidence", "stale"}),
    "evaluating": frozenset({"eligible_for_confirmation", "rejected_by_gate", "insufficient_evidence", "stale"}),
    "eligible_for_confirmation": frozenset({"notified", "approved", "rejected_by_admin", "expired", "stale"}),
    "notified": frozenset({"approved", "rejected_by_admin", "expired", "stale"}),
    "approved": frozenset({"adopting", "creating_pr", "publish_approved", "failed"}),
    "adopting": frozenset({"adopted", "failed", "rolled_back"}),
    "creating_pr": frozenset({"pr_created", "failed"}),
    "pr_created": frozenset({"publish_approved", "failed", "expired"}),
    "publish_approved": frozenset({"publishing", "failed", "expired"}),
    "publishing": frozenset({"published", "failed"}),
    "adopted": frozenset({"rolled_back", "failed"}),
    "published": frozenset({"rolled_back"}),
}


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _iso(value: datetime | None = None) -> str:
    return _now(value).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_code(code: str) -> str:
    return "sha256:" + hashlib.sha256(code.encode("utf-8")).hexdigest()


def _digest(value: Mapping[str, Any] | str) -> str:
    payload = value if isinstance(value, str) else _json(value)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal_id: str
    workspace: str
    skill_name: str
    source_kind: str
    target: str
    status: str
    baseline_revision_id: str | None
    candidate_revision_id: str | None
    baseline_hash: str
    candidate_hash: str
    version_epoch: int
    gate_result: str
    confirmation_expires_at: str | None


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    proposal_id: str
    action: str
    status: str
    result: Mapping[str, Any]


class ProposalConflictError(RuntimeError):
    """Raised when a Proposal no longer matches the caller's expected state."""


ProposalConflict = ProposalConflictError


class DeliveryQuotaExceededError(RuntimeError):
    """Raised when a group has reached its configured daily notification quota."""


class ProposalRepository:
    """Repository implementing Proposal state transitions with SQLite CAS."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create_proposal(
        self,
        *,
        workspace: str,
        skill_name: str,
        source_kind: str,
        target: str,
        baseline_hash: str,
        candidate_hash: str,
        gate_result: str = "passed",
        status: str = "eligible_for_confirmation",
        baseline_revision_id: str | None = None,
        candidate_revision_id: str | None = None,
        skill_id: str | None = None,
        trace_ids: Sequence[str] = (),
        case_ids: Sequence[str] = (),
        eval_run_ids: Sequence[str] = (),
        gate_snapshot: Mapping[str, Any] | None = None,
        proposal_id: str | None = None,
        now: datetime | None = None,
    ) -> ProposalRecord:
        if source_kind not in {"workspace", "builtin", "shared", "entrypoint", "mcp"}:
            raise ValueError("unsupported Proposal source kind")
        if target not in {"workspace_adopt_proposal", "git_pr_proposal"}:
            raise ValueError("unsupported Proposal target")
        if status not in PROPOSAL_STATUSES:
            raise ValueError("unsupported Proposal status")
        if gate_result not in {"passed", "failed", "insufficient_evidence", "stale"}:
            raise ValueError("unsupported gate result")
        proposal_id = proposal_id or f"prop-{uuid4()}"
        timestamp = _iso(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT INTO skill_proposals(proposal_id,workspace,skill_id,skill_name,source_kind,target,"
                "baseline_revision_id,candidate_revision_id,baseline_hash,candidate_hash,version_epoch,"
                "gate_result,gate_snapshot_json,trace_ids_json,case_ids_json,eval_run_ids_json,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, workspace, skill_id, skill_name, source_kind, target, baseline_revision_id,
                 candidate_revision_id, baseline_hash, candidate_hash, 0, gate_result, _json(gate_snapshot or {}),
                 _json(list(trace_ids)), _json(list(case_ids)), _json(list(eval_run_ids)), status, timestamp, timestamp),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get(proposal_id)  # type: ignore[return-value]

    def get(self, proposal_id: str) -> ProposalRecord | None:
        row = self.connection.execute(
            "SELECT proposal_id,workspace,skill_name,source_kind,target,status,baseline_revision_id,"
            "candidate_revision_id,baseline_hash,candidate_hash,version_epoch,gate_result,confirmation_expires_at "
            "FROM skill_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            return None
        return ProposalRecord(*row)

    def list_proposals(
        self,
        *,
        workspace: str,
        statuses: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[ProposalRecord]:
        """List proposals for a workspace without exposing confirmation secrets.

        The command layer uses this read-only projection for ``/evolve list``.
        Statuses are validated here so callers cannot accidentally construct an
        unsafe SQL fragment from user supplied command text.
        """
        if limit < 1 or limit > 100:
            raise ValueError("proposal list limit must be between 1 and 100")
        selected = tuple(statuses or ())
        unknown = set(selected) - PROPOSAL_STATUSES
        if unknown:
            raise ValueError(f"unsupported Proposal status: {sorted(unknown)!r}")
        sql = (
            "SELECT proposal_id,workspace,skill_name,source_kind,target,status,"
            "baseline_revision_id,candidate_revision_id,baseline_hash,candidate_hash,"
            "version_epoch,gate_result,confirmation_expires_at FROM skill_proposals "
            "WHERE workspace=?"
        )
        params: list[Any] = [workspace]
        if selected:
            placeholders = ",".join("?" for _ in selected)
            sql += f" AND status IN ({placeholders})"
            params.extend(selected)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return [ProposalRecord(*row) for row in rows]

    def transition(
        self,
        proposal_id: str,
        *,
        expected_status: str,
        new_status: str,
        expected_epoch: int | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ProposalRecord:
        if new_status not in PROPOSAL_STATUSES or new_status not in _TRANSITIONS.get(expected_status, ()):
            raise ValueError(f"invalid Proposal transition: {expected_status} -> {new_status}")
        timestamp = _iso(now)
        clauses = ["proposal_id=?", "status=?"]
        params: list[Any] = [proposal_id, expected_status]
        if expected_epoch is not None:
            clauses.append("version_epoch=?")
            params.append(expected_epoch)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                "UPDATE skill_proposals SET status=?,version_epoch=version_epoch+1,failure_reason=COALESCE(?,failure_reason),"
                "updated_at=? WHERE " + " AND ".join(clauses),
                [new_status, reason, timestamp, *params],
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise ProposalConflict("Proposal status or version changed")
            self.connection.commit()
        except ProposalConflict:
            raise
        except Exception:
            self.connection.rollback()
            raise
        return self.get(proposal_id)  # type: ignore[return-value]

    def issue_confirmation(
        self,
        proposal_id: str,
        *,
        ttl_minutes: int = 15,
        code: str | None = None,
        now: datetime | None = None,
    ) -> str:
        if ttl_minutes < 1:
            raise ValueError("confirmation TTL must be positive")
        current = self.get(proposal_id)
        if current is None:
            raise KeyError(proposal_id)
        if current.status not in {"eligible_for_confirmation", "notified"}:
            raise ProposalConflict("Proposal is not awaiting confirmation")
        plain_code = code or f"{secrets.randbelow(1_000_000):06d}"
        if not plain_code.isdigit() or not 4 <= len(plain_code) <= 8:
            raise ValueError("confirmation code must be 4-8 digits")
        timestamp = _now(now)
        expires = _iso(timestamp + timedelta(minutes=ttl_minutes))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                "UPDATE skill_proposals SET status='notified',confirmation_code_hash=?,confirmation_expires_at=?,"
                "version_epoch=version_epoch+1,updated_at=? WHERE proposal_id=? AND status IN ('eligible_for_confirmation','notified')",
                (_hash_code(plain_code), expires, _iso(timestamp), proposal_id),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise ProposalConflict("Proposal changed while issuing confirmation")
            self.connection.commit()
        except ProposalConflict:
            raise
        except Exception:
            self.connection.rollback()
            raise
        return plain_code

    def _record_action(
        self,
        *,
        proposal_id: str,
        workspace: str,
        action: str,
        actor_openid: str,
        group_openid: str | None,
        idempotency_key: str,
        request_digest: str,
        result_status: str,
        result: Mapping[str, Any],
        now: datetime | None = None,
    ) -> ActionResult:
        action_id = f"action-{uuid4()}"
        timestamp = _iso(now)
        try:
            self.connection.execute(
                "INSERT INTO proposal_actions(action_id,proposal_id,workspace,action,actor_openid,group_openid,"
                "idempotency_key,request_digest,result_status,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (action_id, proposal_id, workspace, action, actor_openid, group_openid, idempotency_key,
                 request_digest, result_status, _json(result), timestamp),
            )
            self.connection.commit()
        except sqlite3.IntegrityError:
            row = self.connection.execute(
                "SELECT action_id,proposal_id,action,result_status,result_json FROM proposal_actions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise
            return ActionResult(row[0], row[1], row[2], row[3], json.loads(row[4]))
        return ActionResult(action_id, proposal_id, action, result_status, dict(result))

    def approve(
        self,
        proposal_id: str,
        *,
        workspace: str,
        actor_openid: str,
        group_openid: str,
        code: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ActionResult:
        current = self.get(proposal_id)
        if current is None:
            raise KeyError(proposal_id)
        request_digest = _digest({"proposal_id": proposal_id, "action": "approve", "code": code})
        if current.status in {"approved", "adopting", "adopted", "creating_pr", "pr_created"}:
            return self._record_action(proposal_id=proposal_id, workspace=workspace, action="approve",
                                       actor_openid=actor_openid, group_openid=group_openid,
                                       idempotency_key=idempotency_key, request_digest=request_digest,
                                       result_status="idempotent", result={"status": current.status})
        if current.status != "notified":
            raise ProposalConflict("Proposal is not awaiting approval")
        row = self.connection.execute(
            "SELECT confirmation_code_hash,confirmation_expires_at FROM skill_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if row is None or not row[0] or not hmac.compare_digest(row[0], _hash_code(code)):
            raise ProposalConflict("invalid confirmation code")
        if not row[1] or row[1] <= _iso(now):
            self._expire(proposal_id, expected_status="notified", now=now)
            raise ProposalConflict("confirmation code expired")
        timestamp = _iso(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                "UPDATE skill_proposals SET status='approved',approved_by=?,approved_at=?,version_epoch=version_epoch+1,"
                "updated_at=? WHERE proposal_id=? AND status='notified' AND confirmation_code_hash=?",
                (actor_openid, timestamp, timestamp, proposal_id, _hash_code(code)),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                current_after = self.get(proposal_id)
                if current_after is not None and current_after.status in {
                    "approved", "adopting", "adopted", "creating_pr", "pr_created",
                }:
                    return self._record_action(
                        proposal_id=proposal_id,
                        workspace=workspace,
                        action="approve",
                        actor_openid=actor_openid,
                        group_openid=group_openid,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        result_status="idempotent",
                        result={"status": current_after.status},
                        now=now,
                    )
                raise ProposalConflict("Proposal changed while approving")
            self.connection.commit()
        except ProposalConflict:
            raise
        except Exception:
            self.connection.rollback()
            raise
        return self._record_action(proposal_id=proposal_id, workspace=workspace, action="approve",
                                   actor_openid=actor_openid, group_openid=group_openid,
                                   idempotency_key=idempotency_key, request_digest=request_digest,
                                   result_status="approved", result={"status": "approved"}, now=now)

    def reject(
        self,
        proposal_id: str,
        *,
        workspace: str,
        actor_openid: str,
        group_openid: str,
        reason: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ActionResult:
        if not reason.strip():
            raise ValueError("rejection reason is required")
        current = self.get(proposal_id)
        if current is None:
            raise KeyError(proposal_id)
        if current.status not in {"eligible_for_confirmation", "notified"}:
            raise ProposalConflict("Proposal is not awaiting rejection")
        timestamp = _iso(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                "UPDATE skill_proposals SET status='rejected_by_admin',rejected_by=?,rejected_at=?,failure_reason=?,"
                "version_epoch=version_epoch+1,updated_at=? WHERE proposal_id=? AND status IN ('eligible_for_confirmation','notified')",
                (actor_openid, timestamp, reason[:500], timestamp, proposal_id),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise ProposalConflict("Proposal changed while rejecting")
            self.connection.commit()
        except ProposalConflict:
            raise
        except Exception:
            self.connection.rollback()
            raise
        return self._record_action(proposal_id=proposal_id, workspace=workspace, action="reject",
                                   actor_openid=actor_openid, group_openid=group_openid,
                                   idempotency_key=idempotency_key, request_digest=_digest(reason),
                                   result_status="rejected", result={"status": "rejected_by_admin"}, now=now)

    def _expire(self, proposal_id: str, *, expected_status: str, now: datetime | None = None) -> bool:
        cursor = self.connection.execute(
            "UPDATE skill_proposals SET status='expired',version_epoch=version_epoch+1,updated_at=? "
            "WHERE proposal_id=? AND status=?", (_iso(now), proposal_id, expected_status),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def enqueue_delivery(
        self,
        proposal_id: str,
        *,
        workspace: str,
        group_openid: str,
        content_hash: str,
        payload: str | None = None,
        now: datetime | None = None,
    ) -> str:
        delivery_id = f"delivery-{uuid4()}"
        timestamp = _iso(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT delivery_id FROM proposal_deliveries WHERE proposal_id=? AND group_openid=? AND content_hash=?",
                (proposal_id, group_openid, content_hash),
            ).fetchone()
            if row:
                self.connection.commit()
                return row[0]
            scope = self.connection.execute(
                "SELECT notification_enabled,daily_notification_limit FROM group_memory_scopes "
                "WHERE group_openid=?", (group_openid,)
            ).fetchone()
            if scope and int(scope[0]):
                day_start = _iso(_now(now).replace(hour=0, minute=0, second=0, microsecond=0))
                sent_count = self.connection.execute(
                    "SELECT count(*) FROM proposal_deliveries WHERE group_openid=? AND created_at>=? "
                    "AND status IN ('pending','leased','sent','retry_wait')",
                    (group_openid, day_start),
                ).fetchone()[0]
                if int(sent_count) >= int(scope[1]):
                    self.connection.rollback()
                    raise DeliveryQuotaExceededError("group notification quota exceeded")
            self.connection.execute(
                "INSERT INTO proposal_deliveries(delivery_id,proposal_id,workspace,group_openid,content_hash,status,"
                "payload,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                (delivery_id, proposal_id, workspace, group_openid, content_hash, payload,
                 timestamp, timestamp, timestamp),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return delivery_id

    def claim_delivery(
        self,
        *,
        workspace: str,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> sqlite3.Row | None:
        """Lease one due Proposal notification for a bounded delivery worker."""

        current = _now(now)
        now_text = _iso(current)
        lease_text = _iso(current + timedelta(seconds=lease_seconds))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM proposal_deliveries WHERE workspace=? AND "
                "((status IN ('pending','retry_wait') AND next_attempt_at<=?) OR "
                "(status='leased' AND lease_until<=?)) ORDER BY created_at LIMIT 1",
                (workspace, now_text, now_text),
            ).fetchone()
            if row is None:
                self.connection.rollback()
                return None
            self.connection.execute(
                "UPDATE proposal_deliveries SET status='leased',lease_until=?,attempt_count=attempt_count+1,"
                "updated_at=? WHERE delivery_id=?",
                (lease_text, now_text, row["delivery_id"]),
            )
            self.connection.commit()
            return self.connection.execute(
                "SELECT * FROM proposal_deliveries WHERE delivery_id=?", (row["delivery_id"],)
            ).fetchone()
        except Exception:
            self.connection.rollback()
            raise

    def latest_delivery(
        self,
        proposal_id: str,
        *,
        group_openid: str,
    ) -> sqlite3.Row | None:
        """Return the newest durable payload for a Proposal/group pair."""
        return self.connection.execute(
            "SELECT * FROM proposal_deliveries WHERE proposal_id=? AND group_openid=? "
            "ORDER BY created_at DESC LIMIT 1",
            (proposal_id, group_openid),
        ).fetchone()

    def mark_delivery_sent(
        self,
        delivery_id: str,
        *,
        provider_message_id: str | None = None,
        audit_ref: str | None = None,
    ) -> bool:
        cursor = self.connection.execute(
            "UPDATE proposal_deliveries SET status='sent',lease_until=NULL,provider_message_id=?,"
            "audit_ref=?,last_error=NULL,updated_at=? WHERE delivery_id=? AND status='leased'",
            (provider_message_id, audit_ref, _iso(), delivery_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def mark_delivery_retry(
        self,
        delivery_id: str,
        error: str,
        *,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> str:
        current = _now(now)
        row = self.connection.execute(
            "SELECT attempt_count,status FROM proposal_deliveries WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if row is None or row["status"] != "leased":
            return "unchanged"
        attempts = int(row["attempt_count"])
        status = "dead_letter" if attempts >= max_attempts else "retry_wait"
        next_time = current + timedelta(minutes=2 ** max(0, attempts - 1))
        self.connection.execute(
            "UPDATE proposal_deliveries SET status=?,lease_until=NULL,next_attempt_at=?,last_error=?,updated_at=? "
            "WHERE delivery_id=? AND status='leased'",
            (status, _iso(next_time), error[:500], _iso(current), delivery_id),
        )
        self.connection.commit()
        return status

    def upsert_group_scope(
        self,
        *,
        group_openid: str,
        namespace: str,
        observation_enabled: bool = False,
        notification_enabled: bool = False,
        command_require_mention: bool = True,
        daily_notification_limit: int = 3,
        now: datetime | None = None,
    ) -> None:
        if daily_notification_limit < 1:
            raise ValueError("daily notification limit must be positive")
        timestamp = _iso(now)
        self.connection.execute(
            "INSERT INTO group_memory_scopes(group_openid,namespace,observation_enabled,notification_enabled,"
            "command_require_mention,daily_notification_limit,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_openid) DO UPDATE SET namespace=excluded.namespace,"
            "observation_enabled=excluded.observation_enabled,notification_enabled=excluded.notification_enabled,"
            "command_require_mention=excluded.command_require_mention,daily_notification_limit=excluded.daily_notification_limit,"
            "updated_at=excluded.updated_at",
            (group_openid, namespace, int(observation_enabled), int(notification_enabled), int(command_require_mention),
             daily_notification_limit, timestamp, timestamp),
        )
        self.connection.commit()


__all__ = [
    "ActionResult", "DeliveryQuotaExceededError", "ProposalConflict", "ProposalConflictError",
    "ProposalRecord", "ProposalRepository", "PROPOSAL_STATUSES",
]
