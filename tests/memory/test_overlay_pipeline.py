from __future__ import annotations

from types import SimpleNamespace

from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.memory.evolution_orchestrator import CandidateSpec, run_fixture_review_cycle
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.overlay_pipeline import OverlayProposalPipeline
from nanobot.memory.proposal_repository import ProposalRepository


def _setup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = open_maintenance_db(workspace)
    connection.execute(
        "INSERT INTO skills(skill_id,name,source_kind,created_at,updated_at) "
        "VALUES('skill-1','demo','builtin','now','now')"
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES('base-1','skill-1','1','sha256:base','old','test','now')"
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES('candidate-1','skill-1','2','sha256:candidate','new','test','now')"
    )
    connection.commit()
    return workspace, connection


def _spec() -> CandidateSpec:
    cases = tuple(
        {"case_id": f"case-{index:02d}", "prompt": "fixture", "expected": "ok"}
        for index in range(20)
    )
    return CandidateSpec(
        task_key="overlay repeat",
        skill_id="skill-1",
        skill_name="demo",
        source_kind="shared",
        baseline_revision_id="base-1",
        candidate_revision_id="candidate-1",
        baseline_hash="sha256:base",
        candidate_hash="sha256:candidate",
        cases=cases,
        fixture_hash="sha256:fixture",
    )


def test_gate_passed_overlay_handoff_creates_pr_and_notifies(tmp_path):
    workspace, connection = _setup(tmp_path)
    created: list[str] = []
    notified: list[str] = []
    repo = ProposalRepository(connection)

    def create_draft(proposal_id: str):
        created.append(proposal_id)
        repo.transition(proposal_id, expected_status="approved", new_status="creating_pr")
        repo.transition(proposal_id, expected_status="creating_pr", new_status="pr_created")
        return SimpleNamespace(status="pr_created")

    pipeline = OverlayProposalPipeline(
        str(workspace),
        create_draft_pr=create_draft,
        notify_publish_candidate=notified.append,
    )
    config = Phase6RuntimeConfig(enabled=True, draft_pr_enabled=True, max_candidates_per_cycle=1)
    result = run_fixture_review_cycle(
        connection,
        workspace,
        config,
        trace_records=[
            {"trace_id": "trace-1", "summary": "overlay repeat", "outcome": "success"},
            {"trace_id": "trace-2", "summary": "overlay repeat", "outcome": "success"},
            {"trace_id": "trace-3", "summary": "overlay repeat", "outcome": "success"},
        ],
        candidate_specs=[_spec()],
        overlay_pipeline=pipeline,
    )

    assert result.overlay_handoff_statuses == ("pr_created",)
    assert created == list(result.proposal_ids)
    assert notified == list(result.proposal_ids)
    assert connection.execute("SELECT status FROM skill_proposals").fetchone()[0] == "pr_created"
    assert connection.execute(
        "SELECT result_status FROM proposal_actions WHERE action='automatic_gate_approval'"
    ).fetchone()[0] == "approved"
    connection.close()


def test_overlay_handoff_stays_disabled_without_explicit_flag(tmp_path):
    workspace, connection = _setup(tmp_path)
    called: list[str] = []
    pipeline = OverlayProposalPipeline(str(workspace), create_draft_pr=called.append)
    config = Phase6RuntimeConfig(enabled=True, draft_pr_enabled=False, max_candidates_per_cycle=1)
    result = run_fixture_review_cycle(
        connection,
        workspace,
        config,
        trace_records=[
            {"trace_id": "trace-1", "summary": "overlay repeat", "outcome": "success"},
            {"trace_id": "trace-2", "summary": "overlay repeat", "outcome": "success"},
            {"trace_id": "trace-3", "summary": "overlay repeat", "outcome": "success"},
        ],
        candidate_specs=[_spec()],
        overlay_pipeline=pipeline,
    )
    assert result.overlay_handoff_statuses == ("disabled",)
    assert called == []
    assert connection.execute("SELECT status FROM skill_proposals").fetchone()[0] == "eligible_for_confirmation"
    connection.close()


def test_ci_refresh_notifies_once_only_after_all_checks_pass(tmp_path):
    workspace, connection = _setup(tmp_path)
    proposal = ProposalRepository(connection).create_proposal(
        proposal_id="prop-ci-refresh",
        workspace=str(workspace),
        skill_id="skill-1",
        skill_name="demo",
        source_kind="builtin",
        target="git_pr_proposal",
        baseline_hash="sha256:base",
        candidate_hash="sha256:candidate",
        status="pr_created",
    )
    notifications: list[str] = []
    statuses = iter((SimpleNamespace(ci_passed=False, reason="checks pending"), SimpleNamespace(ci_passed=True)))
    pipeline = OverlayProposalPipeline(
        str(workspace),
        create_draft_pr=lambda _proposal_id: SimpleNamespace(status="idempotent"),
        notify_publish_candidate=notifications.append,
        refresh_ci=lambda _proposal_id: next(statuses),
    )

    pending = pipeline.refresh_ci_and_notify(connection, proposal.proposal_id)
    sent = pipeline.refresh_ci_and_notify(connection, proposal.proposal_id)
    replay = pipeline.refresh_ci_and_notify(connection, proposal.proposal_id)

    assert pending.status == "ci_pending"
    assert sent.status == "publish_candidate_notified"
    assert replay.status == "notification_idempotent"
    assert notifications == [proposal.proposal_id]
    assert connection.execute(
        "SELECT COUNT(*) FROM proposal_actions WHERE proposal_id=? AND action='publish_candidate_notification'",
        (proposal.proposal_id,),
    ).fetchone()[0] == 1
    connection.close()


def test_failed_handoff_notifies_and_retries_without_a_second_gate(tmp_path):
    workspace, connection = _setup(tmp_path)
    repo = ProposalRepository(connection)
    proposal = repo.create_proposal(
        proposal_id="prop-retry", workspace=str(workspace), skill_id="skill-1", skill_name="demo",
        source_kind="shared", target="git_pr_proposal", baseline_hash="sha256:base",
        candidate_hash="sha256:candidate", status="eligible_for_confirmation",
    )
    failures: list[tuple[str, str]] = []
    attempts = iter((SimpleNamespace(status="failed", reason="Author identity unknown"), SimpleNamespace(status="pr_created")))
    pipeline = OverlayProposalPipeline(
        str(workspace), create_draft_pr=lambda _proposal_id: next(attempts), notify_failure=lambda *args: failures.append(args)
    )
    config = Phase6RuntimeConfig(enabled=True, draft_pr_enabled=True)

    first = pipeline.handoff(connection, proposal.proposal_id, config)
    assert first.status == "failed"
    assert failures == [(proposal.proposal_id, "Author identity unknown")]
    retried = pipeline.retry_failed_handoffs(connection, config)
    assert [item.status for item in retried] == ["pr_created"]
    assert connection.execute("SELECT status FROM skill_proposals WHERE proposal_id=?", (proposal.proposal_id,)).fetchone()[0] == "approved"
    assert connection.execute(
        "SELECT COUNT(*) FROM proposal_actions WHERE proposal_id=? AND action='automatic_gate_approval'",
        (proposal.proposal_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT result_status FROM proposal_actions WHERE proposal_id=? AND action='retry_overlay_handoff'",
        (proposal.proposal_id,),
    ).fetchone()[0] == "requeued"
    connection.close()
