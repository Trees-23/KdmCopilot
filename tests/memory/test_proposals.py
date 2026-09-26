from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.proposal_repository import ProposalConflict, ProposalRepository


def _repo(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, ProposalRepository(open_maintenance_db(workspace))


def _create(repo: ProposalRepository, workspace: str, **kwargs):
    return repo.create_proposal(
        workspace=workspace,
        skill_name="demo",
        source_kind="workspace",
        target="workspace_adopt_proposal",
        baseline_hash="sha256:base",
        candidate_hash="sha256:candidate",
        trace_ids=["trace-1"],
        case_ids=["case-1"],
        eval_run_ids=["run-1"],
        **kwargs,
    )


def test_proposal_confirmation_is_hashed_and_approval_is_idempotent(tmp_path):
    workspace, repo = _repo(tmp_path)
    proposal = _create(repo, str(workspace))
    code = repo.issue_confirmation(proposal.proposal_id, code="4821")
    assert code == "4821"
    row = repo.connection.execute(
        "SELECT confirmation_code_hash,confirmation_expires_at,status FROM skill_proposals WHERE proposal_id=?",
        (proposal.proposal_id,),
    ).fetchone()
    assert row[0] != code and row[0].startswith("sha256:")
    assert row[1] and row[2] == "notified"

    result = repo.approve(
        proposal.proposal_id, workspace=str(workspace), actor_openid="admin-1", group_openid="group-1",
        code=code, idempotency_key="request-1",
    )
    assert result.status == "approved"
    assert repo.get(proposal.proposal_id).status == "approved"
    repeated = repo.approve(
        proposal.proposal_id, workspace=str(workspace), actor_openid="admin-1", group_openid="group-1",
        code=code, idempotency_key="request-2",
    )
    assert repeated.status == "idempotent"
    assert repeated.result["status"] == "approved"
    assert repo.connection.execute(
        "SELECT count(*) FROM proposal_actions WHERE proposal_id=?", (proposal.proposal_id,)
    ).fetchone()[0] == 2


def test_expired_or_wrong_confirmation_cannot_change_state(tmp_path):
    workspace, repo = _repo(tmp_path)
    proposal = _create(repo, str(workspace))
    issued_at = datetime(2026, 1, 1, tzinfo=UTC)
    repo.issue_confirmation(proposal.proposal_id, code="4821", ttl_minutes=15, now=issued_at)
    with pytest.raises(ProposalConflict, match="invalid confirmation"):
        repo.approve(
            proposal.proposal_id, workspace=str(workspace), actor_openid="admin-1", group_openid="group-1",
            code="0000", idempotency_key="request-wrong", now=issued_at,
        )
    with pytest.raises(ProposalConflict, match="expired"):
        repo.approve(
            proposal.proposal_id, workspace=str(workspace), actor_openid="admin-1", group_openid="group-1",
            code="4821", idempotency_key="request-expired", now=issued_at + timedelta(minutes=16),
        )
    assert repo.get(proposal.proposal_id).status == "expired"


def test_transition_uses_epoch_cas_and_delivery_is_deduplicated(tmp_path):
    workspace, repo = _repo(tmp_path)
    proposal = _create(repo, str(workspace), status="draft")
    moved = repo.transition(proposal.proposal_id, expected_status="draft", new_status="evaluating", expected_epoch=0)
    assert moved.version_epoch == 1
    with pytest.raises(ProposalConflict):
        repo.transition(proposal.proposal_id, expected_status="evaluating", new_status="eligible_for_confirmation", expected_epoch=0)
    first = repo.enqueue_delivery(
        proposal.proposal_id, workspace=str(workspace), group_openid="group-1", content_hash="sha256:msg"
    )
    second = repo.enqueue_delivery(
        proposal.proposal_id, workspace=str(workspace), group_openid="group-1", content_hash="sha256:msg"
    )
    assert first == second
    repo.upsert_group_scope(group_openid="group-1", namespace="qq:group-1", notification_enabled=True)
    row = repo.connection.execute("SELECT notification_enabled,namespace FROM group_memory_scopes WHERE group_openid='group-1'").fetchone()
    assert tuple(row) == (1, "qq:group-1")


def test_concurrent_approvals_have_one_winner_and_no_duplicate_adoption(tmp_path):
    workspace, repo = _repo(tmp_path)
    proposal = _create(repo, str(workspace))
    repo.issue_confirmation(proposal.proposal_id, code="4821")
    database = workspace / ".nanobot" / "memory.sqlite3"

    def approve(request_id: str):
        connection_repo = ProposalRepository(open_maintenance_db(workspace))
        try:
            return connection_repo.approve(
                proposal.proposal_id,
                workspace=str(workspace),
                actor_openid="admin-1",
                group_openid="group-1",
                code="4821",
                idempotency_key=request_id,
            ).status
        except ProposalConflict:
            return "conflict"
        finally:
            connection_repo.connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(approve, ("concurrent-a", "concurrent-b")))
    assert statuses[0] == "approved"
    assert statuses[1] in {"conflict", "idempotent"}
    assert repo.get(proposal.proposal_id).status == "approved"
    assert repo.connection.execute(
        "SELECT count(*) FROM proposal_actions WHERE proposal_id=?", (proposal.proposal_id,)
    ).fetchone()[0] == 2
    assert database.exists()


def test_concurrent_same_idempotency_key_returns_same_record(tmp_path):
    workspace, repo = _repo(tmp_path)
    proposal = _create(repo, str(workspace))
    repo.issue_confirmation(proposal.proposal_id, code="4821")

    def approve():
        connection_repo = ProposalRepository(open_maintenance_db(workspace))
        try:
            return connection_repo.approve(
                proposal.proposal_id,
                workspace=str(workspace),
                actor_openid="admin-1",
                group_openid="group-1",
                code="4821",
                idempotency_key="same-request",
            )
        finally:
            connection_repo.connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve(), (1, 2)))
    assert {result.status for result in results} <= {"approved", "idempotent"}
    assert len({result.action_id for result in results}) == 1
    assert repo.connection.execute(
        "SELECT count(*) FROM proposal_actions WHERE idempotency_key='same-request'"
    ).fetchone()[0] == 1
