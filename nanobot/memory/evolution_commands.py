"""Deterministic ``/evolve`` command handling for the QQ review workflow.

This module is intentionally separate from the LLM turn.  It only reads the
Proposal projection or performs the repository's approval/rejection CAS after
checking the QQ group and administrator allowlists.  Adoption, PR creation,
and publishing are deliberately not performed here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.proposal_repository import (
    ProposalConflict,
    ProposalRepository,
)


@dataclass(frozen=True, slots=True)
class EvolutionCommandResult:
    content: str
    handled: bool = True


class EvolutionCommandService:
    """Authorize and execute one textual ``/evolve`` command."""

    def __init__(self, workspace: str, phase6_config: Any):
        self.workspace = str(workspace)
        self.config = phase6_config

    def _groups(self) -> set[str]:
        cfg = getattr(self.config, "evolution", self.config)
        configured = getattr(cfg, "notification_groups", None)
        if configured is None:
            configured = getattr(cfg, "notificationGroups", ())
        return {str(value) for value in configured or () if str(value)}

    def _admins(self) -> set[str]:
        cfg = getattr(self.config, "evolution", self.config)
        configured = getattr(cfg, "approval_admin_openids", None)
        if configured is None:
            configured = getattr(cfg, "approvalAdminOpenids", ())
        return {str(value) for value in configured or () if str(value)}

    def _require_scope(self, metadata: Mapping[str, Any], *, mutating: bool = False) -> tuple[str, str | None]:
        if str(metadata.get("qq_chat_type") or "") != "group":
            raise PermissionError("/evolve 仅允许在已配置的 QQ 群中使用")
        group = str(metadata.get("group_openid") or metadata.get("chat_id") or "")
        if not group or group not in self._groups():
            raise PermissionError("当前 QQ 群不在进化通知白名单中")
        require_mention = bool(getattr(getattr(self.config, "evolution", self.config), "command_require_mention", True))
        # QQ's group-at callback is the reliable mention signal.  Older
        # adapters may omit it; in that case the command remains compatible.
        if require_mention and metadata.get("qq_mentioned_bot") is False:
            raise PermissionError("请先 @机器人后再执行 /evolve 命令")
        actor = str(metadata.get("sender_openid") or metadata.get("sender_id") or "") or None
        if mutating and actor not in self._admins():
            raise PermissionError("只有配置的审批管理员可以批准或拒绝 Proposal")
        return group, actor

    def _open(self) -> sqlite3.Connection:
        connection = connect_memory_db(self.workspace)
        apply_migrations(connection)
        return connection

    @staticmethod
    def _record_line(record: Any) -> str:
        return f"- {record.proposal_id} | {record.skill_name} | {record.status} | Gate: {record.gate_result}"

    def handle(self, args: str, *, metadata: Mapping[str, Any]) -> EvolutionCommandResult:
        tokens = args.strip().split(maxsplit=3)
        action = tokens[0].lower() if tokens else "status"
        if action not in {"status", "list", "review", "approve", "reject"}:
            return EvolutionCommandResult(
                "用法：/evolve status | list [pending|recent] | review <proposal-id> | "
                "approve <proposal-id> <code> | reject <proposal-id> <reason>"
            )

        mutating = action in {"approve", "reject"}
        try:
            group, actor = self._require_scope(metadata, mutating=mutating)
        except PermissionError as exc:
            return EvolutionCommandResult(f"拒绝：{exc}")

        connection = self._open()
        try:
            repo = ProposalRepository(connection)
            if action == "status":
                evolution = getattr(self.config, "evolution", self.config)
                enabled = bool(getattr(self.config, "enabled", False))
                return EvolutionCommandResult(
                    "Phase 6 状态：{}；主动通知：{}；采用：{}；发布：{}；当前群：{}".format(
                        "开启" if enabled else "关闭",
                        "开启" if bool(getattr(evolution, "notifications_enabled", False)) else "关闭",
                        "开启" if bool(getattr(evolution, "adoption_enabled", False)) else "关闭",
                        "开启" if bool(getattr(evolution, "publish_enabled", False)) else "关闭",
                        group,
                    )
                )

            if action == "list":
                mode = tokens[1].lower() if len(tokens) > 1 else "pending"
                statuses = ("eligible_for_confirmation", "notified") if mode == "pending" else None
                records = repo.list_proposals(workspace=self.workspace, statuses=statuses, limit=20)
                if not records:
                    return EvolutionCommandResult("没有可显示的 Proposal。")
                title = "待确认 Proposal" if statuses else "最近 Proposal"
                return EvolutionCommandResult(title + "：\n" + "\n".join(self._record_line(r) for r in records))

            if len(tokens) < 2:
                return EvolutionCommandResult("缺少 proposal-id。")
            proposal_id = tokens[1]
            record = repo.get(proposal_id)
            if record is None or record.workspace != self.workspace:
                return EvolutionCommandResult("未找到该 Proposal，或它不属于当前工作区。")

            if action == "review":
                expires = record.confirmation_expires_at or "未签发"
                return EvolutionCommandResult(
                    "Proposal 审阅：\n"
                    f"ID：{record.proposal_id}\n"
                    f"Skill：{record.skill_name}\n"
                    f"来源：{record.source_kind}\n"
                    f"目标：{record.target}\n"
                    f"状态：{record.status}\n"
                    f"Gate：{record.gate_result}\n"
                    f"确认码有效期：{expires}\n"
                    "证据正文已脱敏；不会在群内展示确认码哈希或完整模型输出。"
                )

            if action == "approve":
                if len(tokens) < 3:
                    return EvolutionCommandResult("用法：/evolve approve <proposal-id> <code>")
                message_id = str(metadata.get("message_id") or "")
                idem = f"qq:{group}:{actor}:{message_id or proposal_id}:approve"
                try:
                    result = repo.approve(
                        proposal_id,
                        workspace=self.workspace,
                        actor_openid=actor or "unknown",
                        group_openid=group,
                        code=tokens[2],
                        idempotency_key=idem,
                    )
                except (ProposalConflict, ValueError) as exc:
                    return EvolutionCommandResult(f"批准失败：{exc}")
                adoption = bool(getattr(getattr(self.config, "evolution", self.config), "adoption_enabled", False))
                return EvolutionCommandResult(
                    f"Proposal {proposal_id} 已记录管理员批准（状态：{result.status}）。"
                    + ("采用流程已启用，将由受控后台继续处理。" if adoption else "当前采用开关关闭，未修改 Skill。")
                )

            reason = tokens[2] if len(tokens) > 2 else "未提供原因"
            message_id = str(metadata.get("message_id") or "")
            idem = f"qq:{group}:{actor}:{message_id or proposal_id}:reject"
            try:
                result = repo.reject(
                    proposal_id,
                    workspace=self.workspace,
                    actor_openid=actor or "unknown",
                    group_openid=group,
                    reason=reason,
                    idempotency_key=idem,
                )
            except (ProposalConflict, ValueError) as exc:
                return EvolutionCommandResult(f"拒绝失败：{exc}")
            return EvolutionCommandResult(f"Proposal {proposal_id} 已拒绝（状态：{result.status}）。")
        finally:
            connection.close()


__all__ = ["EvolutionCommandResult", "EvolutionCommandService"]
