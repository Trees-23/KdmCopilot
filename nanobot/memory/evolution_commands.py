"""Deterministic ``/evolve`` command handling for the QQ review workflow.

This module is intentionally separate from the LLM turn.  It only reads the
Proposal projection or performs the repository's approval/rejection CAS after
checking the QQ group and administrator allowlists.  Adoption, PR creation,
and publishing are deliberately not performed here.
"""

from __future__ import annotations

import difflib
import json
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

    def _record_line(self, record: Any, *, next_action: bool = False) -> str:
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
        line = (
            f"- Skill：{record.skill_name}\n"
            f"  日期：{date}\n"
            f"  状态：{statuses.get(record.status, record.status)}\n"
            f"  自动评审：{gate}\n"
            f"  编号：{record.public_id}\n"
            f"  查看：/evolve review {record.public_id}"
        )
        if not next_action:
            return line
        if record.status in {"eligible_for_confirmation", "notified"}:
            return line + "\n  下一步：先审阅；如同意，请使用通知中的确认码执行 /evolve approve"
        if record.status == "pr_created":
            enabled = bool(getattr(getattr(self.config, "evolution", self.config), "publish_enabled", False))
            command = f"/evolve publish {record.public_id}"
            if enabled:
                return line + f"\n  下一步：可申请群内二次发布确认：{command}"
            return line + "\n  下一步：可继续审阅；当前发布能力关闭，不能发布。"
        return line

    @staticmethod
    def _list_spec(mode: str) -> tuple[str, tuple[str, ...] | None] | None:
        """Return a user-facing queue title and its explicitly allowed states."""

        specs: dict[str, tuple[str, tuple[str, ...] | None]] = {
            # Compatibility alias: it now means every item that needs an
            # administrator action, not just the first confirmation step.
            "pending": ("等待管理员处理", ("eligible_for_confirmation", "notified", "pr_created")),
            "review": ("等待首次审阅", ("eligible_for_confirmation", "notified")),
            "publish": ("等待最终发布确认", ("pr_created",)),
            "rejected": ("已拒绝或质量拦截", ("rejected_by_admin", "rejected_by_gate", "insufficient_evidence", "stale")),
            "failed": ("失败的 Proposal", ("failed",)),
            "recent": ("最近 Proposal 记录", None),
        }
        return specs.get(mode)

    @staticmethod
    def _menu() -> str:
        return (
            "自进化查询菜单\n\n"
            "/evolve status\n查看模块开关状态\n\n"
            "/evolve list pending\n查看待确认的沉淀 Skill\n\n"

            "/evolve list review\n查看等待首次审阅的候选\n\n"

            "/evolve list publish\n查看已建 Draft PR、等待最终发布确认的候选\n\n"

            "/evolve list rejected | failed\n查看已拒绝/质量拦截或失败的记录\n\n"

            "/evolve recovery\n查看已通过质量门禁的恢复案例（不会自动生成 Skill）\n\n"
            "/evolve list recent\n查看最近的 Proposal\n\n"
            "/evolve overlay\n查看私有 Overlay 中的候选 Skill\n\n"
            "/evolve overlay <Skill 名称>\n查看候选 Skill 正文（仅审批管理员）\n\n"
            "/evolve review <Skill 名称或 Proposal 编号>\n查看详细评审信息\n\n"
            "/evolve review <Proposal 编号> cases\n查看完整评测案例（问题、预期、结果）\n\n"
            "/evolve review <Proposal 编号> diff\n查看候选 Skill 与基线差异\n\n"
            "/evolve reject <Proposal 编号> 原因\n拒绝未发布 Proposal；Overlay Draft 会保持未发布状态\n\n"
            "/evolve menu\n再次显示本菜单\n\n"
            "也可以直接用中文问：\n"
            "“看看候选 Skill”\n"
            "“看看现在有哪些待确认的沉淀 Skill”\n"
            "“看看当前有哪些 Skill”"
        )

    @staticmethod
    def _review_text(details: Mapping[str, Any], mode: str = "summary") -> str:
        record = details["record"]
        snapshot = details["gate_snapshot"]
        public_id = record.public_id
        content = details["candidate_content"]
        if mode == "diff":
            diff = "".join(difflib.unified_diff(
                details["baseline_content"].splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile="baseline/SKILL.md", tofile="candidate/SKILL.md",
            )) or "（基线与候选没有文本差异）"
            return f"Proposal 差异：{public_id}\n\n--- Markdown diff ---\n{diff}"

        runs = details["runs"]
        results = details["results"]
        split_totals: dict[str, list[int]] = {"train": [0, 0], "validation": [0, 0], "holdout": [0, 0]}
        tools: set[str] = set()
        high_risk = 0
        violations = 0
        for row in results:
            split, outcome = str(row[2]), str(row[3])
            if split in split_totals:
                split_totals[split][0] += 1
                split_totals[split][1] += int(outcome == "passed")
            try:
                tools.update(json.loads(row[7] or "[]"))
            except (TypeError, ValueError):
                pass
            high_risk += int(row[8] or 0)
            violations += int(row[9] or 0)
        source = {
            "workspace": "当前工作区", "builtin": "内置 Skill", "shared": "共享 Skill",
            "entrypoint": "入口插件", "mcp": "MCP Skill",
        }.get(record.source_kind, record.source_kind)
        target = "工作区采用" if record.target == "workspace_adopt_proposal" else "私有 Overlay Draft PR"
        status = {
            "eligible_for_confirmation": "待确认", "notified": "已通知待确认", "approved": "已批准",
            "pr_created": "已创建 Draft PR", "published": "已发布", "adopted": "已采用",
            "rolled_back": "已回滚", "rejected_by_admin": "已拒绝", "expired": "已过期", "failed": "失败",
        }.get(record.status, record.status)
        action_values: list[dict[str, Any]] = []
        for action, action_status, result_json, _created in details["actions"]:
            try:
                value = json.loads(result_json or "{}")
            except (TypeError, ValueError):
                value = {}
            action_values.append({"action": action, "status": action_status, **value})
        pr = next((item for item in reversed(action_values) if item["action"] in {"remote_draft_pr", "create_draft_pr"}), None)
        semantic_rows = details.get("semantic", ())
        quality_rows = details.get("quality_reviews", ())
        semantic_goal = str(semantic_rows[0][1]) if semantic_rows else "（历史 Proposal 未保存业务语义）"
        semantic_intent = str(semantic_rows[0][2]) if semantic_rows else "未记录"
        semantic_scope = str(semantic_rows[0][3]) if semantic_rows else "未记录"
        semantic_expected = str(semantic_rows[0][4]) if semantic_rows else "未记录"
        operation_signature = "未记录"
        if semantic_rows:
            try:
                operation_signature = "、".join(json.loads(semantic_rows[0][5] or "[]")) or "未记录"
            except (TypeError, ValueError, json.JSONDecodeError):
                operation_signature = "未记录"
        quality = quality_rows[0] if quality_rows else None
        quality_text = (
            f"{quality[1]}：{quality[3]}" if quality else "历史 Proposal 未记录语义质量 Gate"
        )
        lines = [
            "【Skill 沉淀审批详情】",
            f"编号：{public_id}",
            f"Skill：{record.skill_name}",
            f"创建日期：{_format_beijing_time(record.created_at)}",
            f"来源：{source}",
            f"目标：{target}",
            f"状态：{status}",
            f"自动评审：{'通过' if record.gate_result == 'passed' else '未通过'}",
            f"确认码有效期：{_format_beijing_time(record.confirmation_expires_at)}",
            "",
            "一、这次沉淀解决什么问题",
            f"- 触发证据：{len(details['trace_ids'])} 条 Trace、{len(details['case_ids'])} 个案例",
            f"- 任务目标：{semantic_goal}",
            f"- 任务类型：{semantic_intent}；范围：{semantic_scope}",
            f"- 预期结果：{semantic_expected}",
            "- 为什么值得沉淀：同类业务任务在多个独立 turn 中稳定完成，并形成可复用的范围、操作和结果约束。",
            "- 当前 Skill 指针未因本 Proposal 自动切换。",
            "",
            "二、案例与安全边界",
            f"- 已验证操作：{operation_signature}",
            f"- 评测使用工具：{', '.join(sorted(tools)) if tools else '无记录'}",
            f"- 高风险工具调用：{high_risk} 次",
            f"- 安全违规：{violations} 次",
            f"- EvalRun：{len(runs)} 次",
            "",
            "三、评测结果",
            f"- 评测集：{sum(item[0] for item in split_totals.values())} 条",
            f"- Train：{split_totals['train'][1]}/{split_totals['train'][0]}",
            f"- Validation：{split_totals['validation'][1]}/{split_totals['validation'][0]}",
            f"- Holdout：{split_totals['holdout'][1]}/{split_totals['holdout'][0]}",
            f"- Gate：{'通过' if snapshot.get('holdout_passed', record.gate_result == 'passed') else '未通过'}；安全检查：{'通过' if snapshot.get('security_clean', not violations) else '未通过'}",
            f"- 独立回放：{'已执行' if any(bool(row[7]) for row in runs) else '未记录'}",
            f"- 语义质量 Gate：{quality_text}",
            "",
            "四、实际 Skill 内容",
            "--- SKILL.md ---",
            content or "（未找到候选 Skill 正文）",
        ]
        if pr:
            branch = str(pr.get("branch", ""))
            branch_display = branch if "phase6" not in branch.casefold() else "已创建（历史内部名称不展示）"
            lines.extend(["", "五、Draft PR", f"- 分支：{branch_display}", f"- 提交：{pr.get('commit', '未记录')}"])
            if pr.get("url"):
                lines.append(f"- 链接：{pr['url']}")
            lines.append(f"- CI：{'已通过' if any(item.get('action') == 'remote_pr_status' and item.get('ci_passed') for item in action_values) else '请查看最新 CI 状态'}")
        lines.extend(["", "查看完整评测案例：/evolve review " + public_id + " cases",
                      "查看基线与候选差异：/evolve review " + public_id + " diff",
                      "安全说明：凭据不会展示；确认码哈希、成员身份和模型隐藏推理不会展示。"])
        if "phase6" not in record.proposal_id.casefold() and record.proposal_id != public_id:
            lines.insert(2, f"内部追踪编号：{record.proposal_id}")
        return "\n".join(lines)

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
        if any(word in value for word in ("待发布", "待我发布", "等待发布")):
            return self.handle("list publish", metadata=metadata)
        if any(word in value for word in ("拒绝", "拦截")):
            return self.handle("list rejected", metadata=metadata)
        if any(word in value for word in ("恢复案例", "恢复经验", "怎么修复过")):
            return self.handle("recovery", metadata=metadata)
        if any(word in value for word in ("失败", "报错", "异常")):
            return self.handle("list failed", metadata=metadata)
        if any(word in value for word in ("待确认", "待审核", "待批准", "待审阅", "沉淀")):
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
        if action not in {"status", "list", "menu", "help", "commands", "enum", "overlay", "recovery", "review", "approve", "reject", "rollback", "publish"}:
            return EvolutionCommandResult(
                "用法：/evolve menu | status | list [pending|review|publish|rejected|failed|recent] | overlay [skill-name] | recovery | review <proposal-id> | "
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

            if action == "recovery":
                from nanobot.memory.recovery_evidence import list_recovery_reviews

                cards = list_recovery_reviews(connection, workspace=self.workspace)
                if not cards:
                    return EvolutionCommandResult("当前没有通过质量门禁的恢复案例。")
                labels = {"passed": "可审阅", "insufficient_recovery_evidence": "证据不足"}
                lines = ["恢复案例（仅记录经验，不会自动生成 Skill）："]
                for card in cards:
                    lines.extend([
                        f"- 失败任务：{card['failure_goal']}",
                        f"  失败类别：{card['failure_class']}；纠正方向：{card['correction_goal']}",
                        f"  证据：{card['episode_count']} 个恢复 Episode；状态：{labels.get(str(card['status']), card['status'])}",
                        f"  结论：{card['reason_text']}",
                    ])
                return EvolutionCommandResult("\n".join(lines))

            if action == "list":
                mode = tokens[1].lower() if len(tokens) > 1 else "pending"
                spec = self._list_spec(mode)
                if spec is None:
                    return EvolutionCommandResult(
                        "用法：/evolve list [pending|review|publish|rejected|failed|recent]"
                    )
                title, statuses = spec
                records = repo.list_proposals(workspace=self.workspace, statuses=statuses, limit=20)
                if not records:
                    return EvolutionCommandResult(f"{title}：当前没有可显示的 Proposal。")
                return EvolutionCommandResult(
                    title + "：\n" + "\n".join(
                        self._record_line(item, next_action=mode in {"pending", "review", "publish"})
                        for item in records
                    )
                )

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
            # Commands may use the public display ID; all state transitions use
            # the immutable internal primary key.
            proposal_id = record.proposal_id

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
                            f"Proposal：{record.public_id}\n"
                            f"确认码：{challenge.code}\n"
                            f"有效期：{expires}\n"
                            f"请由同一管理员 @机器人执行：/evolve publish {record.public_id} <确认码>"
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
                        f"Proposal {record.public_id} 已发布到个人 Gateway（状态：published）。"
                    )
                return EvolutionCommandResult(f"发布失败：{result.reason or result.status}")

            if action == "review":
                mode = tokens[2].lower() if len(tokens) > 2 else "summary"
                if mode not in {"summary", "cases", "diff"}:
                    return EvolutionCommandResult("用法：/evolve review <Proposal 编号> [cases|diff]")
                details = repo.review_details(record.proposal_id)
                if details is None:
                    return EvolutionCommandResult("未找到该 Proposal 的评审证据。")
                if mode == "cases":
                    rows = details["cases"]
                    if not rows:
                        return EvolutionCommandResult(f"Proposal {record.public_id} 当前没有可展示的评测案例正文。")
                    lines = [f"Proposal 评测案例：{record.public_id}", ""]
                    for _pack_id, case_key, prompt, expected, split, source_case_id in rows:
                        result = next((item for item in details["results"] if item[1] == case_key), None)
                        lines.extend([
                            f"[{split}] {case_key}（来源案例：{source_case_id}）",
                            f"问题：{prompt}",
                            f"预期：{expected}",
                            f"结果：{result[3] if result else '未记录'}；得分：{result[4] if result else '未记录'}",
                            f"工具：{result[7] if result else '未记录'}",
                            "",
                        ])
                    return EvolutionCommandResult("\n".join(lines).rstrip())
                return EvolutionCommandResult(self._review_text(details, mode))

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
                        f"Proposal {record.public_id} 已批准并采用 Skill（状态：adopted）。"
                    )
                if adopted is not None and adopted.status not in {"disabled", "adopted"}:
                    return EvolutionCommandResult(
                        f"Proposal {record.public_id} 已批准，但采用失败：{adopted.reason or adopted.status}。"
                    )
                return EvolutionCommandResult(
                    f"Proposal {record.public_id} 已记录管理员批准（状态：{result.status}）。"
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
                return EvolutionCommandResult(f"Proposal {record.public_id} 已回滚（状态：rolled_back）。")

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
            return EvolutionCommandResult(f"Proposal {record.public_id} 已拒绝（状态：{result.status}）。")
        finally:
            connection.close()


__all__ = ["EvolutionCommandResult", "EvolutionCommandService", "PublishCallbacks"]
