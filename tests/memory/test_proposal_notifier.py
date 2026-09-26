from types import SimpleNamespace

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.proposal_notifier import ProposalNotifier
from nanobot.memory.proposal_repository import ProposalRepository


def _config(enabled=True):
    return SimpleNamespace(
        evolution=SimpleNamespace(
            notifications_enabled=enabled,
            notification_groups=["group-1"],
            proposal_ttl_minutes=720,
        )
    )


def _seed(tmp_path):
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    repo = ProposalRepository(connection)
    repo.upsert_group_scope(
        group_openid="group-1",
        namespace="qq:group-1",
        observation_enabled=True,
        notification_enabled=True,
        daily_notification_limit=12,
    )
    proposal = repo.create_proposal(
        workspace=str(tmp_path),
        skill_name="demo",
        source_kind="workspace",
        target="workspace_adopt_proposal",
        baseline_hash="base",
        candidate_hash="candidate",
    )
    connection.close()
    return proposal.proposal_id


@pytest.mark.asyncio
async def test_notifier_persists_payload_and_publishes_group_message(tmp_path):
    proposal_id = _seed(tmp_path)
    bus = MessageBus()
    notifier = ProposalNotifier(str(tmp_path), _config(), bus)

    queued = notifier.enqueue(proposal_id, group_openid="group-1")
    assert queued.status == "queued"
    assert "/evolve approve" in (queued.content or "")
    assert notifier.enqueue(proposal_id, group_openid="other").status == "group_not_allowed"

    assert await notifier.deliver_once() == "sent"
    message = await bus.consume_outbound()
    assert message.channel == "qq"
    assert message.chat_id == "group-1"
    assert message.metadata["qq_chat_type"] == "group"
    assert message.metadata["system_notification"] is True

    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    delivery = connection.execute(
        "SELECT status,payload FROM proposal_deliveries WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    assert delivery[0] == "sent"
    assert "/evolve approve" in delivery[1]
    connection.close()


def test_notifier_does_not_send_when_notifications_are_disabled(tmp_path):
    proposal_id = _seed(tmp_path)
    notifier = ProposalNotifier(str(tmp_path), _config(enabled=False), MessageBus())
    assert notifier.enqueue(proposal_id, group_openid="group-1").status == "disabled"
