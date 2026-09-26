"""Durable QQ Proposal notification outbox adapter.

The notifier only queues system messages through ``MessageBus``.  It never
uses the LLM message tool and never enables adoption or publishing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.proposal_repository import (
    ProposalConflict,
    ProposalRepository,
)


@dataclass(frozen=True, slots=True)
class NotificationEnqueueResult:
    status: str
    delivery_id: str | None = None
    content: str | None = None


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class ProposalNotifier:
    """Create and deliver deduplicated Proposal notifications."""

    def __init__(
        self,
        workspace: str,
        config: Any,
        bus: MessageBus,
        *,
        connection_factory: Callable[[str], sqlite3.Connection] = connect_memory_db,
    ):
        self.workspace = str(workspace)
        self.config = config
        self.bus = bus
        self._connection_factory = connection_factory

    def _evolution(self) -> Any:
        return getattr(self.config, "evolution", self.config)

    def _groups(self) -> set[str]:
        return {str(x) for x in getattr(self._evolution(), "notification_groups", ()) or () if str(x)}

    def _enabled(self) -> bool:
        return bool(getattr(self._evolution(), "notifications_enabled", False))

    def _open(self) -> sqlite3.Connection:
        connection = self._connection_factory(self.workspace)
        apply_migrations(connection)
        return connection

    @staticmethod
    def _content(record: Any, code: str) -> str:
        expires = record.confirmation_expires_at or "配置有效期内"
        return (
            "【Skill 升级候选】\n"
            f"提案：{record.proposal_id}\n"
            f"Skill：{record.skill_name}\n"
            f"来源：{record.source_kind}\n"
            f"自动评审：{record.gate_result}\n"
            f"有效期：{expires}\n\n"
            f"查看：/evolve review {record.proposal_id}\n"
            f"同意：/evolve approve {record.proposal_id} {code}\n"
            f"拒绝：/evolve reject {record.proposal_id} 原因"
        )

    def enqueue(self, proposal_id: str, *, group_openid: str) -> NotificationEnqueueResult:
        if not self._enabled():
            return NotificationEnqueueResult("disabled")
        if group_openid not in self._groups():
            return NotificationEnqueueResult("group_not_allowed")
        connection = self._open()
        try:
            repo = ProposalRepository(connection)
            record = repo.get(proposal_id)
            if record is None:
                return NotificationEnqueueResult("not_found")
            content: str | None = None
            if record.status == "eligible_for_confirmation":
                try:
                    code = repo.issue_confirmation(
                        proposal_id,
                        ttl_minutes=int(getattr(self._evolution(), "proposal_ttl_minutes", 720)),
                    )
                except ProposalConflict:
                    record = repo.get(proposal_id)
                    delivery = repo.latest_delivery(proposal_id, group_openid=group_openid)
                    content = str(delivery["payload"] or "") if delivery is not None else ""
                    if not content:
                        return NotificationEnqueueResult("race_retry")
                else:
                    record = repo.get(proposal_id)
                if record is None:
                    return NotificationEnqueueResult("not_found")
                if content is None:
                    content = self._content(record, code)
            elif record.status == "notified":
                delivery = repo.latest_delivery(proposal_id, group_openid=group_openid)
                content = str(delivery["payload"] or "") if delivery is not None else ""
                if not content:
                    return NotificationEnqueueResult("missing_payload")
            else:
                return NotificationEnqueueResult("not_eligible")
            assert content is not None
            delivery_id = repo.enqueue_delivery(
                proposal_id,
                workspace=self.workspace,
                group_openid=group_openid,
                content_hash=_digest(content),
                payload=content,
            )
            return NotificationEnqueueResult("queued", delivery_id, content)
        finally:
            connection.close()

    async def deliver_once(self, *, worker_id: str = "phase6-notifier") -> str:
        """Publish one leased message; retry/dead-letter failures durably."""
        connection = self._open()
        try:
            repo = ProposalRepository(connection)
            row = repo.claim_delivery(workspace=self.workspace, worker_id=worker_id)
            if row is None:
                return "empty"
            payload = str(row["payload"] or "")
            if not payload:
                repo.mark_delivery_retry(row["delivery_id"], "missing notification payload", max_attempts=1)
                return "dead_letter"
            message = OutboundMessage(
                channel="qq",
                chat_id=row["group_openid"],
                content=payload,
                metadata={
                    "qq_chat_type": "group",
                    "group_openid": row["group_openid"],
                    "proposal_id": row["proposal_id"],
                    "proposal_delivery_id": row["delivery_id"],
                    "system_notification": True,
                },
            )
            try:
                await self.bus.publish_outbound(message)
            except Exception as exc:
                status = repo.mark_delivery_retry(row["delivery_id"], str(exc))
                if status == "dead_letter":
                    await self._publish_dead_letter_alert(row, str(exc))
                return status
            repo.mark_delivery_sent(audit_ref=f"bus:{row['delivery_id']}", delivery_id=row["delivery_id"])
            return "sent"
        finally:
            connection.close()

    async def _publish_dead_letter_alert(self, row: Mapping[str, Any], error: str) -> None:
        content = (
            "【Skill 进化通知告警】\n"
            f"Proposal {row['proposal_id']} 的 QQ 通知已进入 dead-letter。\n"
            f"投递：{row['delivery_id']}\n原因：{error[:300]}"
        )
        for group in sorted(self._groups()):
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel="qq",
                    chat_id=group,
                    content=content,
                    metadata={"qq_chat_type": "group", "group_openid": group, "system_alert": True},
                )
            )


__all__ = ["NotificationEnqueueResult", "ProposalNotifier"]
