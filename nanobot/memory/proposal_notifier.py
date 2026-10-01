"""Durable QQ Proposal notification outbox adapter.

The notifier only queues system messages through ``MessageBus``.  It never
uses the LLM message tool and never enables adoption or publishing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping
from uuid import uuid4

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.memory.db import connect_memory_db
from nanobot.memory.evolution_commands import _format_beijing_time
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
    def _safe_failure_reason(reason: str) -> str:
        value = str(reason or "").casefold()
        if "author identity unknown" in value or "unable to auto-detect email" in value:
            return "私有 Overlay 的 Git 提交身份未配置"
        if "worktree must be clean" in value or "overlay checkout is dirty" in value:
            return "私有 Overlay 留有未完成的系统候选文件"
        if any(marker in value for marker in ("ci", "graphql", "statuscheck", "actions")):
            return "私有 Overlay 的 CI 状态检查失败或不可用"
        if "github" in value or "gh " in value:
            return "私有 Overlay 的 GitHub 操作失败"
        return "Skill 进化受控流水线执行失败"

    def enqueue_alert(self, category: str, content: str, *, fingerprint: str) -> int:
        """Persist a deduplicated, payload-free operational alert for QQ delivery."""
        if not self._enabled():
            return 0
        if not category or not fingerprint:
            raise ValueError("alert category and fingerprint are required")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        count = 0
        connection = self._open()
        try:
            for group in sorted(self._groups()):
                alert_id = f"phase6-alert-{uuid4()}"
                try:
                    connection.execute(
                        "INSERT INTO phase6_alert_deliveries(alert_id,workspace,group_openid,category,fingerprint,"
                        "payload,status,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?, 'pending',?,?,?)",
                        (alert_id, self.workspace, group, category, fingerprint, content[:2000], now, now, now),
                    )
                except sqlite3.IntegrityError:
                    continue
                else:
                    count += 1
            connection.commit()
        finally:
            connection.close()
        return count

    def enqueue_pipeline_failure(self, proposal_id: str, reason: str) -> int:
        """Queue one visible alert when a Proposal cannot reach a Draft PR."""
        connection = self._open()
        try:
            record = ProposalRepository(connection).get(proposal_id)
        finally:
            connection.close()
        skill = record.skill_name if record is not None else "未知候选"
        safe_reason = self._safe_failure_reason(reason)
        ci_failure = "CI" in safe_reason
        return self.enqueue_alert(
            "overlay_handoff_failed",
            "【Skill 进化异常】\n"
            f"Skill：{skill}\n"
            f"阶段：私有 Overlay {'CI 核验' if ci_failure else 'Draft PR'}\n"
            f"结果：{'Draft PR 已保留，等待 CI 核验恢复' if ci_failure else '已停止，未创建 PR'}，未影响当前生效 Skill。\n"
            f"原因：{safe_reason}\n"
            f"查看：/evolve review {proposal_id}\n"
            "系统会在下一次受保护扫描中安全重试；如仍失败会保留告警记录。",
            fingerprint=f"{proposal_id}:{safe_reason}",
        )

    def enqueue_scan_failure(self, reason: str) -> int:
        """Queue a visible alert for scan/setup errors without a Proposal row."""
        safe_reason = self._safe_failure_reason(reason)
        return self.enqueue_alert(
            "phase6_scan_failed",
            "【Skill 进化异常】\n"
            "阶段：定时扫描或 Overlay 准备\n"
            "结果：本次未产生可发布变更，当前生效 Skill 未受影响。\n"
            f"原因：{safe_reason}\n"
            "系统将于下一次受保护扫描重试；可用 /evolve status 查看运行状态。",
            fingerprint=safe_reason,
        )

    @staticmethod
    def _content(record: Any, code: str) -> str:
        if getattr(record, "target", None) == "git_pr_proposal":
            return (
                "【Skill 发布候选】\n"
                f"提案：{record.proposal_id}\n"
                f"Skill：{record.skill_name}\n"
                f"来源：{record.source_kind}\n"
                f"自动评审：{record.gate_result}\n"
                "Draft PR：已创建；CI：已通过\n\n"
                f"查看：/evolve review {record.proposal_id}\n"
                f"发布确认：/evolve publish {record.proposal_id}\n"
                "说明：第一次命令签发一次性确认码；同一管理员再次携带确认码执行发布。"
            )
        expires = _format_beijing_time(record.confirmation_expires_at)
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
            elif record.status == "pr_created" and record.target == "git_pr_proposal":
                content = self._content(record, "")
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

    async def deliver_alert_once(self, *, worker_id: str = "gateway-phase6-alerts") -> str:
        """Deliver one durable operational alert with bounded retry."""
        connection = self._open()
        try:
            now = datetime.now(UTC)
            now_text = now.isoformat(timespec="seconds")
            lease_until = (now + timedelta(seconds=60)).isoformat(timespec="seconds")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM phase6_alert_deliveries WHERE workspace=? AND "
                "((status IN ('pending','retry_wait') AND next_attempt_at<=?) OR "
                "(status='leased' AND lease_until<=?)) ORDER BY created_at LIMIT 1",
                (self.workspace, now_text, now_text),
            ).fetchone()
            if row is None:
                connection.rollback()
                return "empty"
            connection.execute(
                "UPDATE phase6_alert_deliveries SET status='leased',attempt_count=attempt_count+1,lease_until=?,updated_at=? "
                "WHERE alert_id=?",
                (lease_until, now_text, row["alert_id"]),
            )
            connection.commit()
            try:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel="qq", chat_id=row["group_openid"], content=str(row["payload"]),
                        metadata={"qq_chat_type": "group", "group_openid": row["group_openid"], "system_alert": True},
                    )
                )
            except Exception as exc:
                current = connection.execute(
                    "SELECT attempt_count FROM phase6_alert_deliveries WHERE alert_id=?", (row["alert_id"],)
                ).fetchone()
                attempts = int(current[0]) if current else 5
                status = "dead_letter" if attempts >= 5 else "retry_wait"
                retry = (now + timedelta(minutes=2 ** max(0, attempts - 1))).isoformat(timespec="seconds")
                connection.execute(
                    "UPDATE phase6_alert_deliveries SET status=?,lease_until=NULL,next_attempt_at=?,last_error=?,updated_at=? "
                    "WHERE alert_id=? AND status='leased'",
                    (status, retry, str(exc)[:500], now_text, row["alert_id"]),
                )
                connection.commit()
                return status
            connection.execute(
                "UPDATE phase6_alert_deliveries SET status='sent',lease_until=NULL,last_error=NULL,updated_at=? "
                "WHERE alert_id=? AND status='leased'",
                (now_text, row["alert_id"]),
            )
            connection.commit()
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
