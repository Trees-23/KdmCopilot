from types import SimpleNamespace

from nanobot.memory.db import connect_memory_db
from nanobot.memory.evolution_commands import EvolutionCommandService
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.proposal_repository import ProposalRepository


def _config(*, enabled=False, admins=("admin-1",), groups=("group-1",)):
    return SimpleNamespace(
        enabled=enabled,
        evolution=SimpleNamespace(
            notification_groups=list(groups),
            approval_admin_openids=list(admins),
            command_require_mention=True,
            notifications_enabled=False,
            adoption_enabled=False,
            publish_enabled=False,
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


def test_evolve_commands_are_deterministic_and_admin_gated(tmp_path):
    proposal_id, code = _proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config())

    status = service.handle("status", metadata=_metadata())
    assert "Phase 6 状态：关闭" in status.content

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
