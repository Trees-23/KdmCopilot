from __future__ import annotations

import json
from datetime import UTC, datetime

import nanobot.memory.phase6_trigger as phase6_trigger
from nanobot.config.schema import Phase6Config
from nanobot.cron.types import CronSchedule
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.phase6_trigger import (
    PHASE6_REVIEW_JOB_ID,
    build_phase6_review_job,
    register_phase6_review_job,
    run_phase6_review_scan,
)
from nanobot.memory.semantic_evidence import persist_turn_semantic_evidence


class _Cron:
    def __init__(self) -> None:
        self.jobs = []

    def register_system_job(self, job) -> None:
        self.jobs.append(job)


def test_builds_protected_daily_beijing_cron_and_skips_disabled_config() -> None:
    job = build_phase6_review_job()
    assert job.id == PHASE6_REVIEW_JOB_ID
    assert job.payload.kind == "system_event"
    assert job.schedule == CronSchedule(kind="cron", expr="0 14 * * *", tz="Asia/Shanghai")

    cron = _Cron()
    assert register_phase6_review_job(cron, Phase6Config(enabled=False)) is False
    assert cron.jobs == []
    assert register_phase6_review_job(cron, Phase6Config(enabled=True)) is True
    assert len(cron.jobs) == 1


def test_production_scan_uses_trace_projection_and_records_cycle(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    connection = open_maintenance_db(workspace)
    now = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO trace_index(trace_id,workspace,started_at,ended_at,outcome,summary,"
        "event_count,tool_count,redaction_version,source_path,created_at,updated_at) "
        "VALUES('t1',?,?,?,?,?,?,?,?,?,?,?)",
        (str(workspace), now, now, "success", "read project status", 1, 1, "v1", "derived://trace", now, now),
    )
    connection.commit()
    connection.close()

    result = run_phase6_review_scan(str(workspace), Phase6Config(enabled=True, min_repeat_count=3))
    assert result.status == "completed"
    evidence = (workspace / ".nanobot" / "phase6" / "evidence.jsonl").read_text(encoding="utf-8")
    assert '"event": "cycle"' in evidence


def test_production_scan_refreshes_trace_index_before_selection(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    audit_root = tmp_path / "audit"
    workspace.mkdir()
    audit_root.mkdir()
    calls: list[tuple[str, str]] = []

    def fake_index(audit_path, workspace_path, *, connection):
        calls.append((str(audit_path), str(workspace_path)))
        return 0

    monkeypatch.setattr(phase6_trigger, "index_audit_root", fake_index)

    result = run_phase6_review_scan(
        workspace,
        Phase6Config(enabled=True),
        audit_root=audit_root,
    )

    assert result.status == "completed"
    assert calls == [(str(audit_root), str(workspace.resolve()))]


def _insert_readonly_traces(workspace, count: int) -> None:
    connection = open_maintenance_db(workspace)
    now = datetime.now(UTC).isoformat()
    summary = json.dumps({"event_types": ["tool_call"], "tool_names": ["read_file"]})
    for index in range(count):
        connection.execute(
            "INSERT INTO trace_index(trace_id,workspace,started_at,ended_at,outcome,summary,"
            "event_count,tool_count,redaction_version,source_path,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"repeat-{index}", str(workspace), now, now, "success", summary, 1, 1,
             "v1", "derived://trace", now, now),
        )
    connection.commit()
    connection.close()


def _insert_semantic_evidence(workspace, count: int) -> None:
    connection = open_maintenance_db(workspace)
    for index in range(count):
        persist_turn_semantic_evidence(
            connection,
            workspace=str(workspace.resolve()),
            trace_id=f"semantic-repeat-{index}",
            turn_id=f"turn-{index}",
            session_key=f"session-{index}",
            source_type="user",
            outcome="success",
            user_text="请查看当前工作区有哪些可用 Skill，并按名称列出",
            tools=("skill_read",),
        )
    connection.close()


def test_production_scan_generates_candidate_proposal_at_three_semantic_repeats(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _insert_semantic_evidence(workspace, 3)

    result = run_phase6_review_scan(str(workspace), Phase6Config(enabled=True))

    assert result.status == "completed"
    assert len(result.proposal_ids) == 1
    connection = open_maintenance_db(workspace)
    proposal = connection.execute(
        "SELECT source_kind,target,status FROM skill_proposals"
    ).fetchone()
    assert tuple(proposal) == ("shared", "git_pr_proposal", "eligible_for_confirmation")
    assert connection.execute("SELECT gate_result FROM eval_runs").fetchone()[0] == "eligible_for_confirmation"
    connection.close()

    # The next daily run sees the same evidence and remains idempotent.
    repeated = run_phase6_review_scan(str(workspace), Phase6Config(enabled=True))
    assert repeated.status == "completed"
    assert repeated.proposal_ids == ()
    assert repeated.deduplicated_task_keys


def test_production_scan_does_not_generate_before_three_repeats(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _insert_readonly_traces(workspace, 2)

    result = run_phase6_review_scan(str(workspace), Phase6Config(enabled=True))

    assert result.status == "completed"
    assert result.selected_tasks == ()
    connection = open_maintenance_db(workspace)
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0
    connection.close()


def test_framework_event_json_never_generates_a_candidate_proposal(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _insert_readonly_traces(workspace, 3)

    result = run_phase6_review_scan(str(workspace), Phase6Config(enabled=True))

    assert result.status == "completed"
    connection = open_maintenance_db(workspace)
    assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0
    connection.close()
