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
        statuses = {
            "eligible_for_confirmation": "待确认",
            "notified": "已通知待确认",
            "approved": "已批准",
            "pr_created": "已创建 Draft PR",
            "published": "已发布",
            "adopted": "已采用",
            "rolled_back": "已回滚",
            "rejected_by_admin": "已拒绝",
            "expired": "已过期",
            "failed": "失败",
        }
        gate = "通过" if record.gate_result == "passed" else "未通过"
        date = _format_beijing_time(record.created_at) if record.created_at else "未知"
        return (
            f"- Skill：{record.skill_name}\n"
            f"  日期：{date}\n"
            f"  状态：{statuses.get(record.status, record.status)}\n"
            f"  自动评审：{gate}\n"
            f"  查看：/evolve review {record.skill_name}"
        )

    @staticmethod
    def _menu() -> str:
        return (
            "自进化查询菜单\n\n"
            "/evolve status\n查看模块开关状态\n\n"
            "/evolve list pending\n查看待确认的沉淀 Skill\n\n"
            "/evolve list recent\n查看最近的 Proposal\n\n"
            "/evolve overlay\n查看私有 Overlay 中的候选 Skill\n\n"
            "/evolve overlay <Skill 名称>\n查看候选 Skill 正文（仅审批管理员）\n\n"
            "/evolve review <Skill 名称或 Proposal 编号>\n查看详细评审信息\n\n"
            "/evolve menu\n再次显示本菜单\n\n"
            "也可以直接用中文问：\n"
            "“看看候选 Skill”\n"
            "“看看现在有哪些待确认的沉淀 Skill”\n"
            "“看看当前有哪些 Skill”"
        )

    def _active_skill_list(self) -> EvolutionCommandResult:
        from nanobot.agent.skills import SkillsLoader

        entries = SkillsLoader(Path(self.workspace)).list_skills(filter_unavailable=False)
        if not entries:
            return EvolutionCommandResult("当前没有可用 Skill。")
        lines = [f"当前可用 Skill（共 {len(entries)} 个）："]
        for entry in entries:
            source = "工作区" if entry["source"] == "workspace" else "内置"
            lines.append(f"- {entry['name']}（{source}）")
        return EvolutionCommandResult("\n".join(lines))

    def handle_natural_query(self, text: str, *, metadata: Mapping[str, Any]) -> EvolutionCommandResult | None:
        """Handle safe, read-only Chinese queries without routing them to mutation commands."""
        value = text.strip().lower()
        if not value or value.startswith("/"):
            return None
        query_words = ("看看", "查看", "查询", "列出", "有哪些", "显示", "告诉我", "想知道")
        if not any(word in value for word in query_words):
            return None
        has_skill_context = any(word in value for word in ("skill", "技能", "沉淀", "候选", "未发布", "overlay"))
        if not has_skill_context and "自进化" not in value:
            return None
        if str(metadata.get("qq_chat_type") or "") != "group":
            return None
        group = str(metadata.get("group_openid") or metadata.get("chat_id") or "")
        if group not in self._groups() or metadata.get("qq_mentioned_bot") is False:
            return None
        if "自进化" in value and not has_skill_context:
            return self.handle("status", metadata=metadata)
        if any(word in value for word in ("待确认", "待审核", "待批准", "沉淀")):
            return self.handle("list pending", metadata=metadata)
        if any(word in value for word in ("候选", "未发布", "overlay")):
            return self.handle("overlay", metadata=metadata)
        if any(word in value for word in ("当前", "至今", "全部", "所有", "有哪些")):
            self._require_scope(metadata)
            return self._active_skill_list()
        return None

    def handle(self, args: str, *, metadata: Mapping[str, Any]) -> EvolutionCommandResult:
        tokens = args.strip().split(maxsplit=3)
        action = tokens[0].lower() if tokens else "status"
        if action not in {"status", "list", "menu", "help", "commands", "enum", "overlay", "review", "approve", "reject", "rollback", "publish"}:
            return EvolutionCommandResult(
                "用法：/evolve menu | status | list [pending|recent] | overlay [skill-name] | review <proposal-id> | "
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
            if action in {"menu", "help", "commands", "enum"}:
                return EvolutionCommandResult(self._menu())
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
                title = "待确认沉淀 Skill" if statuses else "最近沉淀记录"
                return EvolutionCommandResult(title + "：\n" + "\n".join(self._record_line(r) for r in records))

            if len(tokens) < 2:
                return EvolutionCommandResult("缺少 proposal-id。")
            proposal_id = tokens[1]
            if action == "review" and repo.get(proposal_id) is None:
                matches = [item for item in repo.list_proposals(workspace=self.workspace, limit=100)
                           if item.skill_name == proposal_id]
                if len(matches) == 1:
                    proposal_id = matches[0].proposal_id
                elif len(matches) > 1:
                    return EvolutionCommandResult("该 Skill 有多个 Proposal，请使用具体 Proposal 编号。")
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
                status = {
                    "eligible_for_confirmation": "待确认",
                    "notified": "已通知待确认",
                    "approved": "已批准",
                    "pr_created": "已创建 Draft PR",
                    "published": "已发布",
                    "adopted": "已采用",
                    "rolled_back": "已回滚",
                    "rejected_by_admin": "已拒绝",
                    "expired": "已过期",
                    "failed": "失败",
                }.get(record.status, record.status)
                source = {
                    "workspace": "当前工作区",
                    "builtin": "内置 Skill",
                    "shared": "共享 Skill",
                    "entrypoint": "入口插件",
                    "mcp": "MCP Skill",
                }.get(record.source_kind, record.source_kind)
                target = "工作区采用" if record.target == "workspace_adopt_proposal" else "私有 Overlay Draft PR"
                gate = "通过" if record.gate_result == "passed" else "未通过"
                return EvolutionCommandResult(
                    "Proposal 审阅：\n"
                    f"Proposal 编号：{record.proposal_id}\n"
                    f"Skill：{record.skill_name}\n"
                    f"创建日期：{_format_beijing_time(record.created_at)}\n"
                    f"来源：{source}\n"
                    f"目标：{target}\n"
                    f"状态：{status}\n"
                    f"自动评审：{gate}\n"
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
