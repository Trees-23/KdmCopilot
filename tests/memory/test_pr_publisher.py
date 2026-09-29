from __future__ import annotations

import hashlib
import subprocess
from types import SimpleNamespace

from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.pr_publisher import create_draft_pr
from nanobot.memory.proposal_repository import ProposalRepository


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _config(*, enabled: bool = True, adoption: bool = True):
    return SimpleNamespace(
        enabled=enabled,
        kill_switch=False,
        evolution=SimpleNamespace(adoption_enabled=adoption, publish_enabled=False),
    )


def _git(path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=path, check=False, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def _setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "测试管理员")
    _git(repo, "config", "user.email", "test@example.invalid")
    skill = repo / "skills" / "demo"
    skill.mkdir(parents=True)
    baseline = "# Demo\n\nold\n"
    candidate = "# Demo\n\nnew\n"
    (repo / ".gitignore").write_text(".nanobot/\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(baseline, encoding="utf-8")
    _git(repo, "add", "--", ".gitignore", "skills/demo/SKILL.md")
    _git(repo, "commit", "-m", "基线：添加测试 Skill")

    connection = open_maintenance_db(repo)
    connection.execute(
        "INSERT INTO skills(skill_id,name,source_kind,source_path,current_revision_id,current_version,created_at,updated_at) "
        "VALUES('skill-1','demo','builtin',?,?,?,'now','now')",
        (str(skill / "SKILL.md"), "base-1", "1"),
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,status,created_at) "
        "VALUES('base-1','skill-1','1',?,?, 'test','active','now')",
        (_digest(baseline), baseline),
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,status,previous_revision_id,created_at) "
        "VALUES('candidate-1','skill-1','2',?,?, 'test','staging','base-1','now')",
        (_digest(candidate), candidate),
    )
    connection.commit()
    repo_store = ProposalRepository(connection)
    proposal = repo_store.create_proposal(
        proposal_id="prop-shared-test",
        workspace=str(repo.resolve()),
        skill_id="skill-1",
        skill_name="demo",
        source_kind="shared",
        target="git_pr_proposal",
        baseline_revision_id="base-1",
        candidate_revision_id="candidate-1",
        baseline_hash=_digest(baseline),
        candidate_hash=_digest(candidate),
        trace_ids=["trace-1"],
        case_ids=["case-1"],
        eval_run_ids=["eval-1"],
    )
    code = repo_store.issue_confirmation(proposal.proposal_id, code="4821", ttl_minutes=720)
    repo_store.approve(
        proposal.proposal_id, workspace=str(repo.resolve()), actor_openid="admin", group_openid="group",
        code=code, idempotency_key="approve-shared-test",
    )
    return repo, connection


def test_shared_proposal_creates_local_draft_branch_without_publish(tmp_path):
    repo, connection = _setup(tmp_path)
    result = create_draft_pr(connection, "prop-shared-test", repo_path=repo, actor="admin", config=_config())

    assert result.status == "pr_created"
    assert result.branch == "evolve/prop-shared-test"
    assert result.commit and len(result.commit) == 40
    assert "## 改动内容" in result.body
    assert "## 评测证据" in result.body
    assert "## 验证结果" in result.body
    assert "## 风险与注意事项" in result.body
    assert "old" not in result.body and "new" not in result.body
    assert "模型" not in result.body
    assert tuple(connection.execute(
        "SELECT p.status,s.current_revision_id FROM skill_proposals p JOIN skills s ON s.skill_id=p.skill_id "
        "WHERE p.proposal_id='prop-shared-test'"
    ).fetchone()) == ("pr_created", "base-1")
    assert (repo / "skills/demo/SKILL.md").read_text(encoding="utf-8") == "# Demo\n\nnew\n"
    assert _git(repo, "log", "-1", "--pretty=%s").stdout.startswith("功能（Skill进化）")
    connection.close()


def test_draft_pr_is_idempotent_and_gates_are_independent(tmp_path):
    repo, connection = _setup(tmp_path)
    first = create_draft_pr(connection, "prop-shared-test", repo_path=repo, actor="admin", config=_config())
    second = create_draft_pr(connection, "prop-shared-test", repo_path=repo, actor="admin", config=_config())
    assert first.status == "pr_created"
    assert second.status == "idempotent"
    assert second.branch == first.branch
    assert _git(repo, "log", "--oneline").stdout.count("功能（Skill进化）") == 1

    disabled = _setup(tmp_path / "disabled")
    assert create_draft_pr(disabled[1], "prop-shared-test", repo_path=disabled[0], actor="admin",
                           config=_config(adoption=False)).status == "disabled"
    disabled[1].close()
    connection.close()


def test_workspace_proposal_cannot_create_draft_pr(tmp_path):
    repo, connection = _setup(tmp_path)
    connection.execute("UPDATE skill_proposals SET source_kind='workspace',target='workspace_adopt_proposal' WHERE proposal_id=?",
                       ("prop-shared-test",))
    connection.commit()
    result = create_draft_pr(connection, "prop-shared-test", repo_path=repo, actor="admin", config=_config())
    assert result.status == "not_pr_proposal"
    connection.close()
