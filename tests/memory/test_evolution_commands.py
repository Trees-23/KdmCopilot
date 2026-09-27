import re
from types import SimpleNamespace

from nanobot.memory.db import connect_memory_db
from nanobot.memory.evolution_commands import (
    EvolutionCommandService,
    PublishCallbacks,
    _format_beijing_time,
)
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.proposal_repository import ProposalRepository


def _config(*, enabled=False, publish_enabled=False, admins=("admin-1",), groups=("group-1",)):
    return SimpleNamespace(
        enabled=enabled,
        evolution=SimpleNamespace(
            notification_groups=list(groups),
            approval_admin_openids=list(admins),
            command_require_mention=True,
            notifications_enabled=False,
            adoption_enabled=False,
            publish_enabled=publish_enabled,
        ),
    )


def _metadata(*, sender="member-1", group="group-1", mention=True, message_id="m-1"):
    return {
        "qq_chat_type": "group",
        "group_openid": group,
        "sender_openid": sender,
        "qq_mentioned_bot": mention,
        "message_id": message_id,
    }


def test_confirmation_expiry_is_rendered_as_beijing_time():
    assert _format_beijing_time("2026-09-27T05:25:47+00:00") == (
        "2026年09月27日 13:25:47（北京时间）"
    )


def _proposal(tmp_path):
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    repo = ProposalRepository(connection)
    repo.upsert_group_scope(
        group_openid="group-1",
        namespace="qq:group-1",
        observation_enabled=True,
        notification_enabled=True,
        command_require_mention=True,
        daily_notification_limit=12,
    )
    proposal = repo.create_proposal(
        workspace=str(tmp_path),
        skill_name="demo-skill",
        source_kind="workspace",
        target="workspace_adopt_proposal",
        baseline_hash="base",
        candidate_hash="candidate",
    )
    code = repo.issue_confirmation(proposal.proposal_id, code="4821", ttl_minutes=720)
    connection.close()
    return proposal.proposal_id, code


def _published_proposal(tmp_path):
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    repo = ProposalRepository(connection)
    proposal = repo.create_proposal(
        proposal_id="prop-publish-command",
        workspace=str(tmp_path),
        skill_name="overlay-skill",
        source_kind="shared",
        target="git_pr_proposal",
        baseline_hash="sha256:base",
        candidate_hash="sha256:candidate",
    )
    code = repo.issue_confirmation(proposal.proposal_id, code="4821", ttl_minutes=720)
    repo.approve(
        proposal.proposal_id,
        workspace=str(tmp_path),
        actor_openid="admin-1",
        group_openid="group-1",
        code=code,
        idempotency_key="approve-publish-command",
    )
    repo.transition(proposal.proposal_id, expected_status="approved", new_status="creating_pr")
    repo.transition(proposal.proposal_id, expected_status="creating_pr", new_status="pr_created")
    repo._record_action(  # type: ignore[attr-defined]
        proposal_id=proposal.proposal_id,
        workspace=str(tmp_path),
        action="create_draft_pr",
        actor_openid="admin-1",
        group_openid=None,
        idempotency_key="draft-publish-command",
        request_digest="sha256:draft",
        result_status="pr_created",
        result={"branch": "evolve/prop-publish-command", "commit": "abc123"},
    )
    connection.close()
    return proposal.proposal_id


def test_evolve_commands_are_deterministic_and_admin_gated(tmp_path):
    proposal_id, code = _proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config())

    status = service.handle("status", metadata=_metadata())
    assert "自进化模块状态" in status.content
    assert "- 总开关：关闭" in status.content
    assert "E217" not in status.content

    review = service.handle(f"review {proposal_id}", metadata=_metadata())
    assert proposal_id in review.content
    assert "确认码哈希" in review.content

    denied = service.handle(
        f"approve {proposal_id} {code}",
        metadata=_metadata(sender="member-1"),
    )
    assert denied.content.startswith("拒绝：")

    approved = service.handle(
        f"approve {proposal_id} {code}",
        metadata=_metadata(sender="admin-1", message_id="approve-1"),
    )
    assert "已记录管理员批准" in approved.content
    assert "未修改 Skill" in approved.content


def test_evolve_rejects_wrong_group_and_missing_mention(tmp_path):
    proposal_id, _ = _proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config())

    wrong_group = service.handle("list", metadata=_metadata(group="other-group"))
    assert wrong_group.content.startswith("拒绝：")

    missing_mention = service.handle(
        f"review {proposal_id}", metadata=_metadata(mention=False)
    )
    assert "@机器人" in missing_mention.content


def test_evolve_reject_is_idempotent_for_replayed_message(tmp_path):
    proposal_id, _ = _proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config())
    metadata = _metadata(sender="admin-1", message_id="reject-1")

    first = service.handle(f"reject {proposal_id} bad candidate", metadata=metadata)
    second = service.handle(f"reject {proposal_id} bad candidate", metadata=metadata)
    assert "已拒绝" in first.content
    assert "拒绝失败" in second.content or "已拒绝" in second.content


def test_publish_confirmation_is_group_admin_only_and_runs_in_two_steps(tmp_path):
    proposal_id = _published_proposal(tmp_path)
    calls: list[str] = []
    service = EvolutionCommandService(
        str(tmp_path),
        _config(enabled=True, publish_enabled=True),
        publish_callbacks=PublishCallbacks(
            ci_passed=lambda _proposal_id: True,
            merge=lambda branch, commit: calls.append(f"merge:{branch}:{commit}") or "merge-ref",
            deploy=lambda ref: calls.append(f"deploy:{ref}") or "deploy-ref",
        ),
    )
    denied = service.handle(f"publish {proposal_id}", metadata=_metadata(sender="member-1"))
    assert denied.content.startswith("拒绝：")

    challenge = service.handle(f"publish {proposal_id}", metadata=_metadata(sender="admin-1"))
    assert "群内二次发布确认已签发" in challenge.content
    code = re.search(r"确认码：([0-9]{4,8})", challenge.content).group(1)
    published = service.handle(
        f"publish {proposal_id} {code}", metadata=_metadata(sender="admin-1", message_id="publish-1")
    )
    assert "已发布到个人 Gateway" in published.content
    assert calls == ["merge:evolve/prop-publish-command:abc123", "deploy:merge-ref"]


def test_publish_command_stays_closed_by_default(tmp_path):
    proposal_id = _published_proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config(enabled=True))
    result = service.handle(f"publish {proposal_id}", metadata=_metadata(sender="admin-1"))
    assert "发布能力当前关闭" in result.content
