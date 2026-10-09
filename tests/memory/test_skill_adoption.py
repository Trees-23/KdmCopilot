from types import SimpleNamespace

from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.proposal_repository import ProposalRepository
from nanobot.memory.skill_adoption import (
    adopt_workspace_proposal,
    issue_rollback_confirmation,
    rollback_workspace_proposal,
)


def _config(enabled=True, adoption=True):
    return SimpleNamespace(
        enabled=enabled,
        kill_switch=False,
        evolution=SimpleNamespace(adoption_enabled=adoption, workspace_skill_allowlist=["demo"]),
    )


def _setup(tmp_path):
    workspace = tmp_path / "workspace"
    skill_path = workspace / "skills" / "demo"
    skill_path.mkdir(parents=True)
    baseline = "# Demo\n\nold\n"
    candidate = "# Demo\n\nnew\n"
    (skill_path / "SKILL.md").write_text(baseline, encoding="utf-8")
    connection = open_maintenance_db(workspace)
    connection.execute(
        "INSERT INTO skills(skill_id,name,source_kind,source_path,current_revision_id,current_version,created_at,updated_at) "
        "VALUES('skill-1','demo','workspace',?,?,?, 'now','now')",
        (str(skill_path / "SKILL.md"), "base-1", "1"),
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,status,created_at) "
        "VALUES('base-1','skill-1','1','sha256:ac8c7c4b7c2e6d36b640e4f18f6db9b4e96f1d6ca1ce8b7f3b0b9eec2e50f8ad',?,'test','active','now')",
        (baseline,),
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,status,previous_revision_id,created_at) "
        "VALUES('candidate-1','skill-1','2','sha256:8d6c0f3d4d8125f4ebc02f2f0b44f9ec14e0e7c6e7e6a4b3c5e1f8b4a0b1c2d3',?,'test','staging','base-1','now')",
        (candidate,),
    )
    # Replace fixture digests with the actual values while keeping the setup readable.
    import hashlib
    for revision_id, content in (("base-1", baseline), ("candidate-1", candidate)):
        digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        connection.execute("UPDATE skill_revisions SET content_hash=? WHERE revision_id=?", (digest, revision_id))
    connection.commit()
    repo = ProposalRepository(connection)
    proposal = repo.create_proposal(
        proposal_id="prop-adopt-test",
        workspace=str(workspace.resolve()),
        skill_id="skill-1",
        skill_name="demo",
        source_kind="workspace",
        target="workspace_adopt_proposal",
        baseline_revision_id="base-1",
        candidate_revision_id="candidate-1",
        baseline_hash="sha256:" + hashlib.sha256(baseline.encode()).hexdigest(),
        candidate_hash="sha256:" + hashlib.sha256(candidate.encode()).hexdigest(),
    )
    code = repo.issue_confirmation(proposal.proposal_id, code="4821", ttl_minutes=720)
    repo.approve(
        proposal.proposal_id,
        workspace=str(workspace.resolve()),
        actor_openid="admin",
        group_openid="group",
        code=code,
        idempotency_key="approve-adopt-test",
    )
    return workspace, connection, code


def test_workspace_adoption_is_atomic_and_rolls_back(tmp_path):
    workspace, connection, code = _setup(tmp_path)
    adopted = adopt_workspace_proposal(
        connection, workspace, "prop-adopt-test", actor="admin", config=_config()
    )
    assert adopted.status == "adopted"
    assert (workspace / "skills/demo/SKILL.md").read_text() == "# Demo\n\nnew\n"
    assert connection.execute("SELECT current_revision_id FROM skills WHERE skill_id='skill-1'").fetchone()[0] == "candidate-1"
    assert connection.execute("SELECT status FROM skills WHERE skill_id='skill-1'").fetchone()[0] == "active"

    rolled_back = rollback_workspace_proposal(
        connection, workspace, "prop-adopt-test", actor="admin", code=code, config=_config()
    )
    assert rolled_back.status == "rolled_back"
    assert (workspace / "skills/demo/SKILL.md").read_text() == "# Demo\n\nold\n"
    assert connection.execute("SELECT current_revision_id FROM skills WHERE skill_id='skill-1'").fetchone()[0] == "base-1"
    assert connection.execute("SELECT status FROM skills WHERE skill_id='skill-1'").fetchone()[0] == "active"
    connection.close()


def test_adoption_stays_disabled_without_explicit_gate(tmp_path):
    workspace, connection, _code = _setup(tmp_path)
    result = adopt_workspace_proposal(
        connection, workspace, "prop-adopt-test", actor="admin", config=_config(adoption=False)
    )
    assert result.status == "disabled"
    assert (workspace / "skills/demo/SKILL.md").read_text() == "# Demo\n\nold\n"
    connection.close()


def test_adoption_rejects_changed_workspace_file(tmp_path):
    workspace, connection, _code = _setup(tmp_path)
    (workspace / "skills/demo/SKILL.md").write_text("# changed\n", encoding="utf-8")
    result = adopt_workspace_proposal(
        connection, workspace, "prop-adopt-test", actor="admin", config=_config()
    )
    assert result.status == "stale"
    connection.close()


def test_rollback_confirmation_is_durable_and_adoption_audit_is_written(tmp_path):
    workspace, connection, _code = _setup(tmp_path)
    adopted = adopt_workspace_proposal(
        connection, workspace, "prop-adopt-test", actor="system", config=_config()
    )
    assert adopted.status == "adopted"
    actions = {
        row[0] for row in connection.execute(
            "SELECT action FROM proposal_actions WHERE proposal_id='prop-adopt-test'"
        )
    }
    assert "workspace_adoption" in actions

    challenge = issue_rollback_confirmation(
        connection, "prop-adopt-test", actor="admin", config=_config()
    )
    assert challenge.status == "issued"
    replay = issue_rollback_confirmation(
        connection, "prop-adopt-test", actor="admin", config=_config()
    )
    assert replay.status == "idempotent"
    assert replay.expires_at == challenge.expires_at
    rolled_back = rollback_workspace_proposal(
        connection,
        workspace,
        "prop-adopt-test",
        actor="admin",
        code=challenge.code or "",
        config=_config(),
    )
    assert rolled_back.status == "rolled_back"
    assert "workspace_rollback" in {
        row[0] for row in connection.execute(
            "SELECT action FROM proposal_actions WHERE proposal_id='prop-adopt-test'"
        )
    }
    connection.close()
