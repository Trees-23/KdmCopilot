from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.memory.evolution_schedule import EvolutionReviewScheduler, load_schedule


def test_scheduler_is_explicit_and_persists_due_state(tmp_path):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    scheduler = EvolutionReviewScheduler(tmp_path, Phase6RuntimeConfig(enabled=True), interval_seconds=7200)
    calls: list[str] = []

    def runner():
        calls.append("run")
        return {"status": "completed"}

    assert scheduler.run_due(runner, now=now)["status"] == "completed"
    assert calls == ["run"]
    assert scheduler.run_due(runner, now=now + timedelta(minutes=1))["status"] == "not_due"
    assert calls == ["run"]
    assert scheduler.run_due(runner, now=now + timedelta(hours=2))["status"] == "completed"
    assert calls == ["run", "run"]
    assert load_schedule(tmp_path).run_count == 2


def test_scheduler_disabled_does_not_create_schedule(tmp_path):
    scheduler = EvolutionReviewScheduler(tmp_path, Phase6RuntimeConfig(), interval_seconds=7200)
    assert scheduler.run_due(lambda: {"status": "should-not-run"})["status"] == "disabled"
    assert not (tmp_path / ".nanobot" / "phase6" / "schedule.json").exists()
