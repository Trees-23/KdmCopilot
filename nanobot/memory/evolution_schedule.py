"""Durable, explicitly-triggered Phase 6 review schedule state."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

from nanobot.memory.continuous import Phase6RuntimeConfig, phase6_active

SCHEDULE_FILE = "schedule.json"


@dataclass(frozen=True, slots=True)
class EvolutionScheduleState:
    interval_seconds: int = 2 * 60 * 60
    next_due_at: str | None = None
    last_run_at: str | None = None
    run_count: int = 0


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _iso(value: datetime | None = None) -> str:
    return _now(value).isoformat(timespec="seconds")


def _path(workspace: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve() / ".nanobot" / "phase6"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / SCHEDULE_FILE


def load_schedule(workspace: str | Path) -> EvolutionScheduleState:
    path = _path(workspace)
    if not path.exists():
        return EvolutionScheduleState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        interval = int(payload.get("interval_seconds", 2 * 60 * 60))
        if interval < 1:
            raise ValueError("invalid interval")
        return EvolutionScheduleState(interval, payload.get("next_due_at"), payload.get("last_run_at"),
                                      int(payload.get("run_count", 0)))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return EvolutionScheduleState()


def save_schedule(workspace: str | Path, state: EvolutionScheduleState) -> None:
    path = _path(workspace)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(asdict(state), handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        temporary = Path(handle.name)
    temporary.replace(path)


class EvolutionReviewScheduler:
    """A scheduler state machine whose execution must be invoked explicitly."""

    def __init__(self, workspace: str | Path, config: Phase6RuntimeConfig, *, interval_seconds: int = 2 * 60 * 60):
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        self.workspace = Path(workspace).expanduser().resolve()
        self.config = config
        self.interval_seconds = interval_seconds

    def run_due(self, runner: Callable[[], Any], *, now: datetime | None = None) -> Any:
        """Run the supplied cycle only when enabled and due; never starts a background task."""

        if not phase6_active(self.config):
            return {"status": "disabled"}
        current = _now(now)
        state = load_schedule(self.workspace)
        if state.next_due_at is None:
            state = EvolutionScheduleState(self.interval_seconds, _iso(current), state.last_run_at, state.run_count)
            save_schedule(self.workspace, state)
        if state.next_due_at and datetime.fromisoformat(state.next_due_at).astimezone(UTC) > current:
            return {"status": "not_due", "next_due_at": state.next_due_at}
        result = runner()
        status = str(getattr(result, "status", None) or (result.get("status") if isinstance(result, dict) else "completed"))
        if status == "locked":
            return result
        save_schedule(
            self.workspace,
            EvolutionScheduleState(
                self.interval_seconds,
                _iso(current + timedelta(seconds=self.interval_seconds)),
                _iso(current),
                state.run_count + 1,
            ),
        )
        return result


__all__ = ["EvolutionReviewScheduler", "EvolutionScheduleState", "load_schedule", "save_schedule"]
