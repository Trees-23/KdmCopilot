"""Automatic Overlay Proposal to Draft PR handoff.

The evaluator creates a Proposal; this adapter is the controlled handoff that
turns an automatically passed Overlay Gate into a private Draft PR.  It never
merges, deploys, or enables publishing.  Host-side callbacks provide the
repository-specific PR creation and QQ notification implementations.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from nanobot.memory.proposal_repository import ProposalConflict, ProposalRepository


@dataclass(frozen=True, slots=True)
class OverlayHandoffResult:
    status: str
    proposal_id: str
    reason: str | None = None


class OverlayProposalPipeline:
    """Perform the non-publishing Overlay handoff after automatic review.

    A repository adapter may return ``ci_passed=False`` while it waits for
    GitHub checks.  In that case the Proposal remains ``pr_created`` and the
    publish-candidate notification is deferred until the adapter's CI refresh
    callback observes a passing result.
    """

    def __init__(
        self,
        workspace: str,
        *,
        create_draft_pr: Callable[[str], Any],
        notify_publish_candidate: Callable[[str], Any] | None = None,
        notify_failure: Callable[[str, str], Any] | None = None,
        refresh_ci: Callable[[str], Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.create_draft_pr = create_draft_pr
        self.notify_publish_candidate = notify_publish_candidate
        self.notify_failure = notify_failure
        self.refresh_ci = refresh_ci

    def _notify_failure(self, proposal_id: str, reason: str | None) -> None:
        if self.notify_failure is None:
            return
        try:
            self.notify_failure(proposal_id, str(reason or "overlay handoff failed")[:300])
        except Exception:
            # The Proposal failure has already been persisted.  Alert enqueue
            # failure must not hide it or alter the protected state machine.
            return

    @staticmethod
    def _enabled(config: Any) -> bool:
        evolution = getattr(config, "evolution", config)
        return bool(getattr(config, "enabled", False)) and not bool(
            getattr(config, "kill_switch", False)
        ) and bool(getattr(evolution, "draft_pr_enabled", False))

    def handoff(self, connection: sqlite3.Connection, proposal_id: str, config: Any) -> OverlayHandoffResult:
        if not self._enabled(config):
            return OverlayHandoffResult("disabled", proposal_id)
        repo = ProposalRepository(connection)
        record = repo.get(proposal_id)
        if record is None:
            return OverlayHandoffResult("not_found", proposal_id)
        if record.target != "git_pr_proposal":
            return OverlayHandoffResult("not_overlay", proposal_id)
        try:
            repo.approve_after_gate(
                proposal_id,
                workspace=self.workspace,
                reason="automatic evaluation Gate passed; awaiting publish confirmation",
            )
            result = self.create_draft_pr(proposal_id)
            result_status = str(getattr(result, "status", ""))
            if result_status not in {"pr_created", "idempotent"}:
                current = repo.get(proposal_id)
                if current and current.status in {"approved", "creating_pr"}:
                    repo.transition(
                        proposal_id,
                        expected_status=current.status,
                        new_status="failed",
                        reason=str(getattr(result, "reason", None) or result_status),
                    )
                reason = str(getattr(result, "reason", None) or result_status)
                self._notify_failure(proposal_id, reason)
                return OverlayHandoffResult("failed", proposal_id, reason)
            # Local fixture adapters do not expose CI state and retain the
            # historical immediate notification behavior.  A real remote
            # adapter must explicitly report ``ci_passed=True`` so that the
            # group is not asked to publish before CI completes.
            ci_passed = getattr(result, "ci_passed", None)
            if self.notify_publish_candidate is not None and (ci_passed is None or ci_passed):
                self.notify_publish_candidate(proposal_id)
            return OverlayHandoffResult("pr_created", proposal_id)
        except (ProposalConflict, sqlite3.Error, OSError, RuntimeError) as exc:
            current = repo.get(proposal_id)
            if current and current.status in {"approved", "creating_pr"}:
                try:
                    repo.transition(proposal_id, expected_status=current.status, new_status="failed", reason=str(exc))
                except (ProposalConflict, sqlite3.Error):
                    pass
            reason = str(exc)[:300]
            self._notify_failure(proposal_id, reason)
            return OverlayHandoffResult("failed", proposal_id, reason)

    def retry_failed_handoffs(self, connection: sqlite3.Connection, config: Any) -> tuple[OverlayHandoffResult, ...]:
        """Retry durable failed handoffs after a later safe runtime repair."""
        repo = ProposalRepository(connection)
        rows = connection.execute(
            "SELECT proposal_id FROM skill_proposals WHERE workspace=? AND target='git_pr_proposal' "
            "AND status='failed' AND gate_result='passed' ORDER BY updated_at LIMIT 20",
            (self.workspace,),
        ).fetchall()
        outcomes: list[OverlayHandoffResult] = []
        for row in rows:
            proposal_id = str(row[0])
            if repo.requeue_failed_overlay_handoff(proposal_id, workspace=self.workspace):
                outcomes.append(self.handoff(connection, proposal_id, config))
        return tuple(outcomes)

    def refresh_ci_and_notify(
        self,
        connection: sqlite3.Connection,
        proposal_id: str,
    ) -> OverlayHandoffResult:
        """Poll remote CI and emit one idempotent publish-candidate notification.

        This is intentionally a separate worker step from Draft PR creation:
        an open PR is not a publish candidate until every required check passes.
        The durable action record prevents repeated polling from sending the QQ
        notification more than once.
        """

        if self.refresh_ci is None:
            return OverlayHandoffResult("ci_not_configured", proposal_id)
        try:
            already_sent = connection.execute(
                "SELECT 1 FROM proposal_actions WHERE proposal_id=? AND action=? LIMIT 1",
                (proposal_id, "publish_candidate_notification"),
            ).fetchone()
            if already_sent:
                return OverlayHandoffResult("notification_idempotent", proposal_id)
            status = self.refresh_ci(proposal_id)
            if not bool(getattr(status, "ci_passed", False)):
                if str(getattr(status, "status", "")) in {"unavailable", "mismatch", "not_found"}:
                    reason = str(getattr(status, "reason", None) or "Overlay CI state unavailable")
                    self._notify_failure(proposal_id, f"CI validation: {reason}")
                    return OverlayHandoffResult("ci_unavailable", proposal_id, reason)
                return OverlayHandoffResult("ci_pending", proposal_id, getattr(status, "reason", None))
            if self.notify_publish_candidate is None:
                return OverlayHandoffResult("ci_passed", proposal_id)
            self.notify_publish_candidate(proposal_id)
            record = ProposalRepository(connection).get(proposal_id)
            if record is None:
                return OverlayHandoffResult("not_found", proposal_id)
            ProposalRepository(connection)._record_action(  # type: ignore[attr-defined]
                proposal_id=proposal_id,
                workspace=record.workspace,
                action="publish_candidate_notification",
                actor_openid="skill-evolution-overlay-notifier",
                group_openid=None,
                idempotency_key=f"publish-candidate-notification:{proposal_id}",
                request_digest="sha256:" + hashlib.sha256(proposal_id.encode()).hexdigest(),
                result_status="sent",
                result={"status": "ci_passed"},
            )
            return OverlayHandoffResult("publish_candidate_notified", proposal_id)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            return OverlayHandoffResult("notification_failed", proposal_id, str(exc)[:300])


__all__ = ["OverlayHandoffResult", "OverlayProposalPipeline"]
