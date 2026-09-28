"""Automatic Overlay Proposal to Draft PR handoff.

The evaluator creates a Proposal; this adapter is the controlled handoff that
turns an automatically passed Overlay Gate into a private Draft PR.  It never
merges, deploys, or enables publishing.  Host-side callbacks provide the
repository-specific PR creation and QQ notification implementations.
"""

from __future__ import annotations

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
    """Perform the non-publishing Overlay handoff after automatic review."""

    def __init__(
        self,
        workspace: str,
        *,
        create_draft_pr: Callable[[str], Any],
        notify_publish_candidate: Callable[[str], Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.create_draft_pr = create_draft_pr
        self.notify_publish_candidate = notify_publish_candidate

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
                return OverlayHandoffResult("failed", proposal_id, getattr(result, "reason", None))
            if self.notify_publish_candidate is not None:
                self.notify_publish_candidate(proposal_id)
            return OverlayHandoffResult("pr_created", proposal_id)
        except (ProposalConflict, sqlite3.Error, OSError, RuntimeError) as exc:
            current = repo.get(proposal_id)
            if current and current.status in {"approved", "creating_pr"}:
                try:
                    repo.transition(proposal_id, expected_status=current.status, new_status="failed", reason=str(exc))
                except (ProposalConflict, sqlite3.Error):
                    pass
            return OverlayHandoffResult("failed", proposal_id, str(exc)[:300])


__all__ = ["OverlayHandoffResult", "OverlayProposalPipeline"]
