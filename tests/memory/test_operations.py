from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nanobot.config.schema import Phase6Config, Phase6EvolutionConfig
from nanobot.memory.continuous import Phase6RuntimeConfig, load_state
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.operations import (
    build_operational_report,
    pause_on_operational_breach,
    render_operational_report,
    review_phase6_configuration,
)


def test_weekly_report_aggregates_redacted_operational_metadata(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = open_maintenance_db(workspace)
    start = datetime(2026, 9, 21, tzinfo=UTC)
    stamp = start.isoformat()
    later = (start + timedelta(hours=1)).isoformat()
    for proposal_id, status, gate, reason in (
        ("p1", "adopted", "passed", None),
        ("p2", "rejected_by_admin", "passed", "误报：仅一次出现"),
        ("p3", "rolled_back", "failed", "回归"),
    ):
        connection.execute(
            "INSERT INTO skill_proposals(proposal_id,workspace,skill_name,source_kind,target,"
            "baseline_hash,candidate_hash,gate_result,status,failure_reason,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, str(workspace), "demo", "workspace", "workspace_adopt_proposal",
             f"b-{proposal_id}", f"c-{proposal_id}", gate, status, reason, stamp, later),
        )
    connection.execute(
        "INSERT INTO proposal_deliveries(delivery_id,proposal_id,workspace,group_openid,content_hash,status,"
        "next_attempt_at,created_at,updated_at) VALUES('d1','p1',?,'g','h','sent',?,?,?)",
        (str(workspace), stamp, stamp, stamp),
    )
    connection.execute(
        "INSERT INTO proposal_deliveries(delivery_id,proposal_id,workspace,group_openid,content_hash,status,"
        "next_attempt_at,created_at,updated_at) VALUES('d2','p2',?,'g','h2','dead_letter',?,?,?)",
        (str(workspace), stamp, stamp, stamp),
    )
    connection.execute(
        "INSERT INTO proposal_actions(action_id,proposal_id,workspace,action,actor_openid,group_openid,"
        "idempotency_key,request_digest,result_status,created_at) VALUES('a1','p2',?,'reject','admin','g','i1','d','rejected',?)",
        (str(workspace), stamp),
    )
    connection.commit()

    report = build_operational_report(connection, since=start, until=start + timedelta(days=7))
    assert report.candidates == 3
    assert report.gate_passed == 2 and report.gate_pass_rate == 2 / 3
    assert report.dead_letters == 1 and report.notification_failure_rate == 0.5
    assert report.admin_rejections == 1 and report.adopted == 1 and report.rolled_back == 1
    rendered = render_operational_report(report)
    assert "误报：仅一次出现" in rendered
    assert "token" not in rendered
    connection.close()


def test_operational_breach_pauses_and_notifies_without_writing_skill(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = type("Report", (), {
        "as_metrics": lambda self: {
            "notifications": 2,
            "notification_failures": 2,
            "notification_failure_rate": 1.0,
            "dead_letter_count": 3,
        },
    })()
    notices: list[str] = []
    config = Phase6RuntimeConfig(enabled=True)
    state = pause_on_operational_breach(str(workspace), config, report, notify=notices.append)
    assert state.paused is True
    assert "dead_letter_threshold" in (state.pause_reason or "")
    assert notices and "自动暂停" in notices[0]
    assert load_state(workspace).paused is True


def test_configuration_review_catches_dangerous_partial_enablement() -> None:
    config = Phase6Config(
        enabled=True,
        kill_switch=True,
        evolution=Phase6EvolutionConfig(
            notifications_enabled=True,
            adoption_enabled=True,
            publish_enabled=True,
            notification_groups=[],
            approval_admin_openids=[],
            overlay_repository="",
        ),
    )
    findings = review_phase6_configuration(config)
    assert findings == (
        "notifications_enabled_without_group_whitelist",
        "adoption_enabled_without_admin_allowlist",
        "publish_enabled_without_overlay_repository",
        "kill_switch_active",
    )
