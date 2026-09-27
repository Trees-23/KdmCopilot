from __future__ import annotations

from types import SimpleNamespace

from nanobot.memory.pr_publisher import create_draft_pr
from nanobot.memory.publish_gate import issue_publish_confirmation, publish_proposal
from tests.memory.test_pr_publisher import _config, _setup


def _publish_config(*, enabled: bool = True, kill_switch: bool = False):
    return SimpleNamespace(
        enabled=True,
        kill_switch=kill_switch,
        evolution=SimpleNamespace(adoption_enabled=True, publish_enabled=enabled),
    )


def test_publish_requires_ci_and_second_confirmation_then_runs_callbacks(tmp_path):
    repo, connection = _setup(tmp_path)
    draft = create_draft_pr(connection, "prop-shared-test", repo_path=repo, actor="admin", config=_config())
    assert draft.status == "pr_created"
    challenge = issue_publish_confirmation(connection, "prop-shared-test", actor="admin", code="654321")
    assert challenge.status == "issued"
    assert challenge.code == "654321"
    replay = issue_publish_confirmation(connection, "prop-shared-test", actor="admin", code="111111")
    assert replay.status == "idempotent"
    assert replay.code is None

    assert publish_proposal(
        connection, "prop-shared-test", actor="admin", code="654321", config=_publish_config(), ci_passed=False,
        merge=lambda _branch, _commit: "merge-ref", deploy=lambda _ref: "deploy-ref",
    ).status == "ci_failed"
    assert publish_proposal(
        connection, "prop-shared-test", actor="admin", code="000000", config=_publish_config(), ci_passed=True,
        merge=lambda _branch, _commit: "merge-ref", deploy=lambda _ref: "deploy-ref",
    ).status == "invalid_code"

    calls: list[str] = []
    result = publish_proposal(
        connection, "prop-shared-test", actor="admin", code="654321", config=_publish_config(), ci_passed=True,
        merge=lambda branch, commit: calls.append(f"merge:{branch}:{commit}") or "merge-ref",
        deploy=lambda ref: calls.append(f"deploy:{ref}") or "deploy-ref",
    )
    assert result.status == "published"
    assert calls[0].startswith("merge:evolve/prop-shared-test:")
    assert calls[1] == "deploy:merge-ref"
    assert connection.execute("SELECT status FROM skill_proposals WHERE proposal_id=?", ("prop-shared-test",)).fetchone()[0] == "published"
    connection.close()


def test_publish_stays_disabled_or_killed_and_never_calls_callbacks(tmp_path):
    repo, connection = _setup(tmp_path)
    assert create_draft_pr(connection, "prop-shared-test", repo_path=repo, actor="admin", config=_config()).status == "pr_created"
    issue_publish_confirmation(connection, "prop-shared-test", actor="admin", code="654321")
    called: list[str] = []
    def callback(*_args):
        called.append("called")
        return "ref"
    assert publish_proposal(
        connection, "prop-shared-test", actor="admin", code="654321", config=_publish_config(enabled=False), ci_passed=True,
        merge=callback, deploy=callback,
    ).status == "disabled"
    assert publish_proposal(
        connection, "prop-shared-test", actor="admin", code="654321", config=_publish_config(kill_switch=True), ci_passed=True,
        merge=callback, deploy=callback,
    ).status == "killed"
    assert called == []
    assert connection.execute("SELECT status FROM skill_proposals WHERE proposal_id=?", ("prop-shared-test",)).fetchone()[0] == "pr_created"
    connection.close()
