"""Deterministic ``/evolve`` command handling for the QQ review workflow.

This module is intentionally separate from the LLM turn.  It only reads the
Proposal projection or performs the repository's approval/rejection CAS after
checking the QQ group and administrator allowlists.  Adoption, PR creation,
and publishing are deliberately not performed here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.overlay_sync import OverlayInspection, inspect_overlay
from nanobot.memory.proposal_repository import (
    ProposalConflict,
    ProposalRepository,
)
from nanobot.memory.publish_gate import issue_publish_confirmation, publish_proposal
from nanobot.memory.skill_adoption import adopt_workspace_proposal, rollback_workspace_proposal


@dataclass(frozen=True, slots=True)
class EvolutionCommandResult:
    content: str
    handled: bool = True


@dataclass(frozen=True, slots=True)
class PublishCallbacks:
    """Explicit CI/merge/deploy hooks for a personal Overlay publish."""

    ci_passed: Callable[[str], bool]
    merge: Callable[[str, str], str]
    deploy: Callable[[str], str]


def _format_beijing_time(value: str | None) -> str:
    """Render an internal UTC timestamp in a human-readable Beijing time."""
    if not value:
        return "未签发"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        return local.strftime("%Y年%m月%d日 %H:%M:%S（北京时间）")
    except (TypeError, ValueError):
        return value


class EvolutionCommandService:
    """Authorize and execute one textual ``/evolve`` command."""

    def __init__(self, workspace: str, phase6_config: Any, *, publish_callbacks: PublishCallbacks | None = None):
        self.workspace = str(workspace)
        self.config = phase6_config
        self.publish_callbacks = publish_callbacks

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
            raise PermissionError("只有配置的审批管理员可以执行变更操作")
        return group, actor

    def _open(self) -> sqlite3.Connection:
        connection = connect_memory_db(self.workspace)
        apply_migrations(connection)
        return connection

    def _overlay_path(self) -> Path:
        evolution = getattr(self.config, "evolution", self.config)
        configured = getattr(evolution, "overlay_checkout_path", None)
        if configured:
            return Path(str(configured)).expanduser().resolve()
        return Path(self.workspace).expanduser().resolve().parent / "skill-evolution-overlay"

    @staticmethod
    def _overlay_status(status: str) -> str:
        return {
            "in_sync": "已同步到生效目录",
            "missing_in_workspace": "未发布（生效目录没有）",
            "drifted": "内容不一致（未确认）",
        }.get(status, status)

    def _overlay_command(self, tokens: list[str], *, actor: str | None) -> EvolutionCommandResult:
        inspection: OverlayInspection = inspect_overlay(
            self._overlay_path(), self.workspace, expected_branch="main"
        )
        if inspection.status in {"invalid", "branch_mismatch"}:
            return EvolutionCommandResult(f"Overlay 读取失败：{inspection.reason or inspection.status}")
        if not inspection.skills:
            return EvolutionCommandResult("候选 Overlay 当前没有可读取的 Skill。")
        if len(tokens) == 1:
            lines = ["候选 Skill（仅 Overlay，不代表已发布）："]
            for skill in inspection.skills:
                lines.append(f"- {skill.skill_name}：{self._overlay_status(skill.status)}")
            lines.append("查看内容：/evolve overlay <skill-name>（仅审批管理员）")
            return EvolutionCommandResult("\n".join(lines))

        if actor not in self._admins():
            return EvolutionCommandResult("拒绝：查看候选 Skill 正文仅允许审批管理员。")
        name = tokens[1]
        selected = next((item for item in inspection.skills if item.skill_name == name), None)
        if selected is None:
            return EvolutionCommandResult(f"未找到候选 Skill：{name}")
        content = Path(selected.overlay_path).read_text(encoding="utf-8")
        limit = 4000
        if len(content) > limit:
            content = content[:limit] + "\n…（正文过长，已截断）"
        return EvolutionCommandResult(
            f"候选 Skill：{selected.skill_name}\n"
            f"状态：{self._overlay_status(selected.status)}\n"
            "说明：这是私有 Overlay 中的候选版本，尚未自动加载到 Agent。\n\n"
            f"--- SKILL.md ---\n{content}"
        )

    @staticmethod
    def _record_line(record: Any) -> str:
        return f"- {record.proposal_id} | {record.skill_name} | {record.status} | Gate: {record.gate_result}"

    def handle(self, args: str, *, metadata: Mapping[str, Any]) -> EvolutionCommandResult:
        tokens = args.strip().split(maxsplit=3)
        action = tokens[0].lower() if tokens else "status"
        if action not in {"status", "list", "overlay", "review", "approve", "reject", "rollback", "publish"}:
            return EvolutionCommandResult(
                "用法：/evolve status | list [pending|recent] | overlay [skill-name] | review <proposal-id> | "
                "approve <proposal-id> <code> | reject <proposal-id> <reason> | "
                "rollback <proposal-id> <code> | publish <proposal-id> [code]"
            )

        mutating = action in {"approve", "reject", "rollback", "publish"}
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
                    "自进化模块状态\n"
                    f"- 总开关：{'开启' if enabled else '关闭'}\n"
                    f"- 主动通知：{'开启' if bool(getattr(evolution, 'notifications_enabled', False)) else '关闭'}\n"
                    f"- 人工采用：{'开启' if bool(getattr(evolution, 'adoption_enabled', False)) else '关闭'}\n"
                    f"- 发布能力：{'开启' if bool(getattr(evolution, 'publish_enabled', False)) else '关闭'}\n"
                    "- 当前群：已通过群范围校验"
                )

            if action == "overlay":
                return self._overlay_command(tokens, actor=actor)

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

            if action == "publish":
                evolution = getattr(self.config, "evolution", self.config)
                if not bool(getattr(evolution, "publish_enabled", False)):
                    return EvolutionCommandResult("发布能力当前关闭，未签发二次确认码。")
                if record.target != "git_pr_proposal" or record.status not in {"pr_created", "published"}:
                    return EvolutionCommandResult("发布拒绝：仅允许个人 Overlay 的 pr_created Proposal。")
                if record.status == "published":
                    return EvolutionCommandResult(f"Proposal {proposal_id} 已发布（幂等结果）。")
                if len(tokens) < 3:
                    challenge = issue_publish_confirmation(
                        connection, proposal_id, actor=actor or "unknown",
                        ttl_minutes=int(getattr(evolution, "proposal_ttl_minutes", 720)),
                    )
                    if challenge.status == "issued":
                        expires = _format_beijing_time(challenge.expires_at)
                        return EvolutionCommandResult(
                            "群内二次发布确认已签发\n"
                            f"Proposal：{proposal_id}\n"
                            f"确认码：{challenge.code}\n"
                            f"有效期：{expires}\n"
                            f"请由同一管理员 @机器人执行：/evolve publish {proposal_id} <确认码>"
                        )
                    if challenge.status == "idempotent":
                        return EvolutionCommandResult(
                            f"该 Proposal 已签发过二次确认码（有效期：{_format_beijing_time(challenge.expires_at)}）。"
                        )
                    return EvolutionCommandResult(f"发布确认失败：{challenge.reason or challenge.status}")
                if self.publish_callbacks is None:
                    return EvolutionCommandResult("发布失败：个人 Gateway 发布回调尚未配置。")
                callbacks = self.publish_callbacks
                result = publish_proposal(
                    connection, proposal_id, actor=actor or "unknown", code=tokens[2], config=self.config,
                    ci_passed=bool(callbacks.ci_passed(proposal_id)), merge=callbacks.merge,
                    deploy=callbacks.deploy,
                )
                if result.status == "published":
                    return EvolutionCommandResult(
                        f"Proposal {proposal_id} 已发布到个人 Gateway（状态：published）。"
                    )
                return EvolutionCommandResult(f"发布失败：{result.reason or result.status}")

            if action == "review":
                expires = _format_beijing_time(record.confirmation_expires_at)
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
                adopted = None
                if adoption and result.status == "approved":
                    adopted = adopt_workspace_proposal(
                        connection, self.workspace, proposal_id, actor=actor or "unknown", config=self.config
                    )
                if adopted is not None and adopted.status == "adopted":
                    return EvolutionCommandResult(
                        f"Proposal {proposal_id} 已批准并采用 Skill（状态：adopted）。"
                    )
                if adopted is not None and adopted.status not in {"disabled", "adopted"}:
                    return EvolutionCommandResult(
                        f"Proposal {proposal_id} 已批准，但采用失败：{adopted.reason or adopted.status}。"
                    )
                return EvolutionCommandResult(
                    f"Proposal {proposal_id} 已记录管理员批准（状态：{result.status}）。"
                    + ("采用流程已启用，将由受控后台继续处理。" if adoption else "当前采用开关关闭，未修改 Skill。")
                )

            if action == "rollback":
                if len(tokens) < 3:
                    return EvolutionCommandResult("用法：/evolve rollback <proposal-id> <code>")
                result = rollback_workspace_proposal(
                    connection,
                    self.workspace,
                    proposal_id,
                    actor=actor or "unknown",
                    code=tokens[2],
                    config=self.config,
                )
                if result.status != "rolled_back":
                    return EvolutionCommandResult(f"回滚失败：{result.reason or result.status}")
                return EvolutionCommandResult(f"Proposal {proposal_id} 已回滚（状态：rolled_back）。")

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


__all__ = ["EvolutionCommandResult", "EvolutionCommandService", "PublishCallbacks"]
