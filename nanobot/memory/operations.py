"""Phase 6 steady-state operations and weekly review helpers.

The functions in this module only read the redacted Proposal projections and
delivery/action metadata.  They produce an operational report and translate
unsafe delivery or authorization signals into the existing Phase 6 pause
state; they never adopt, publish, or modify a Skill.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from nanobot.memory.continuous import Phase6State, update_health


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class OperationalReport:
    """Bounded, payload-free metrics for one review window."""

    window_start: str
    window_end: str
    candidates: int
    gate_evaluated: int
    gate_passed: int
    notifications: int
    notification_failures: int
    dead_letters: int
    admin_rejections: int
    admin_approvals: int
    adopted: int
    rolled_back: int
    rejection_reasons: tuple[str, ...] = ()

    @property
    def gate_pass_rate(self) -> float:
        return self.gate_passed / self.gate_evaluated if self.gate_evaluated else 0.0

    @property
    def notification_failure_rate(self) -> float:
        return self.notification_failures / self.notifications if self.notifications else 0.0

    def as_metrics(self) -> dict[str, Any]:
        """Return only scalar metrics suitable for health evidence."""

        return {
            "candidates": self.candidates,
            "gate_evaluated": self.gate_evaluated,
            "gate_passed": self.gate_passed,
            "gate_pass_rate": self.gate_pass_rate,
            "notifications": self.notifications,
            "notification_failures": self.notification_failures,
            "notification_failure_rate": self.notification_failure_rate,
            "dead_letter_count": self.dead_letters,
            "admin_rejections": self.admin_rejections,
            "admin_approvals": self.admin_approvals,
            "adopted": self.adopted,
            "rolled_back": self.rolled_back,
        }


def build_operational_report(
    connection: sqlite3.Connection,
    *,
    since: datetime,
    until: datetime | None = None,
) -> OperationalReport:
    """Build a weekly report from Proposal metadata, never provider payloads."""

    end = until or datetime.now(UTC)
    start_text, end_text = _iso(since), _iso(end)
    window = (start_text, end_text)
    candidates, gate_evaluated, gate_passed = connection.execute(
        "SELECT count(*), count(gate_result), "
        "sum(CASE WHEN gate_result='passed' THEN 1 ELSE 0 END) "
        "FROM skill_proposals WHERE created_at>=? AND created_at<?",
        window,
    ).fetchone()
    notifications, failures, dead_letters = connection.execute(
        "SELECT count(*), sum(CASE WHEN status IN ('retry_wait','dead_letter') THEN 1 ELSE 0 END), "
        "sum(CASE WHEN status='dead_letter' THEN 1 ELSE 0 END) "
        "FROM proposal_deliveries WHERE created_at>=? AND created_at<?",
        window,
    ).fetchone()
    admin_rejections, admin_approvals = connection.execute(
        "SELECT sum(CASE WHEN action='reject' THEN 1 ELSE 0 END), "
        "sum(CASE WHEN action='approve' THEN 1 ELSE 0 END) "
        "FROM proposal_actions WHERE created_at>=? AND created_at<?",
        window,
    ).fetchone()
    adopted, rolled_back = connection.execute(
        "SELECT sum(CASE WHEN status='adopted' THEN 1 ELSE 0 END), "
        "sum(CASE WHEN status='rolled_back' THEN 1 ELSE 0 END) "
        "FROM skill_proposals WHERE updated_at>=? AND updated_at<?",
        window,
    ).fetchone()
    reasons = tuple(
        str(row[0])[:160]
        for row in connection.execute(
            "SELECT failure_reason FROM skill_proposals "
            "WHERE status='rejected_by_admin' AND updated_at>=? AND updated_at<? "
            "AND failure_reason IS NOT NULL AND failure_reason<>'' ORDER BY updated_at LIMIT 50",
            window,
        )
    )
    return OperationalReport(
        start_text,
        end_text,
        int(candidates or 0),
        int(gate_evaluated or 0),
        int(gate_passed or 0),
        int(notifications or 0),
        int(failures or 0),
        int(dead_letters or 0),
        int(admin_rejections or 0),
        int(admin_approvals or 0),
        int(adopted or 0),
        int(rolled_back or 0),
        reasons,
    )


def render_operational_report(report: OperationalReport) -> str:
    """Render a concise Chinese report safe for administrator notification."""

    reasons = "、".join(report.rejection_reasons[:5]) if report.rejection_reasons else "无"
    return (
        "【Skill 进化周报】\n"
        f"窗口：{report.window_start} ～ {report.window_end}\n"
        f"候选数：{report.candidates}\n"
        f"Gate 通过率：{report.gate_pass_rate:.1%}（{report.gate_passed}/{report.gate_evaluated}）\n"
        f"通知失败率：{report.notification_failure_rate:.1%}（{report.notification_failures}/{report.notifications}）\n"
        f"dead-letter：{report.dead_letters}\n"
        f"管理员拒绝/批准：{report.admin_rejections}/{report.admin_approvals}\n"
        f"采用/回滚：{report.adopted}/{report.rolled_back}\n"
        f"拒绝原因：{reasons}"
    )


def pause_on_operational_breach(
    workspace: str,
    config: Any,
    report: OperationalReport,
    *,
    unauthorized_attempts: int = 0,
    cross_group_leaks: int = 0,
    notify: Callable[[str], None] | None = None,
) -> Phase6State:
    """Pause Phase 6 when operational thresholds are breached.

    ``notify`` is deliberately injected so QQ delivery remains an adapter
    concern.  A failed notification cannot prevent the durable pause state.
    """

    metrics = report.as_metrics()
    metrics.update({
        "unauthorized_attempts": int(unauthorized_attempts),
        "cross_group_leaks": int(cross_group_leaks),
    })
    state = update_health(workspace, metrics, config)
    if state.paused and notify is not None:
        notify(
            "【Skill 进化自动暂停】\n"
            f"原因：{state.pause_reason}\n"
            "新的扫描、通知、采用、Draft PR 和发布已停止；请管理员复核后手动恢复。"
        )
    return state


__all__ = [
    "OperationalReport",
    "build_operational_report",
    "pause_on_operational_breach",
    "render_operational_report",
]
