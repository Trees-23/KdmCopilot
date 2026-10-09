"""Single-worker orchestration for maintenance review batches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from nanobot.memory.lock import LockLease
from nanobot.memory.maintenance import (
    MaintenanceJob,
    claim_due_job,
    complete_job,
    fail_job,
    open_maintenance_db,
    process_one_outbox,
)


@dataclass(frozen=True, slots=True)
class ReviewResult:
    review_cursor: str
    snapshot_cursor: str


ReviewCallback = Callable[[MaintenanceJob], ReviewResult]


class MaintenanceWorker:
    """Process at most one workspace job while holding one renewable lease."""

    def __init__(self, workspace: str | Path, *, worker_id: str | None = None) -> None:
        self.workspace = str(Path(workspace).resolve())
        self.worker_id = worker_id or f"maintenance:{uuid4()}"
        self._lease: LockLease | None = None

    def run_once(self, review: ReviewCallback | None = None) -> str:
        connection = open_maintenance_db(self.workspace)
        try:
            from nanobot.memory.lock import acquire_lock, release_lock

            lease = acquire_lock(connection, self.workspace, self.worker_id, lease_seconds=90)
            if lease is None:
                return "locked"
            self._lease = lease
            job: MaintenanceJob | None = None
            try:
                job = claim_due_job(connection, workspace=self.workspace, worker_id=self.worker_id)
                if job is None:
                    process_one_outbox(connection, workspace=self.workspace, worker_id=self.worker_id)
                    return "idle"
                callback = review or self._default_review
                result = callback(job)
                if not complete_job(
                    connection, job_id=job.job_id, worker_id=self.worker_id,
                    epoch=job.activity_epoch, review_cursor=result.review_cursor,
                    snapshot_cursor=result.snapshot_cursor,
                ):
                    return "stale"
                return "done"
            except Exception as exc:
                if job is None:
                    return "error"
                return fail_job(
                    connection, job_id=job.job_id, worker_id=self.worker_id,
                    error=str(exc),
                )
            finally:
                release_lock(connection, lease)
                self._lease = None
        finally:
            connection.close()

    @staticmethod
    def _default_review(job: MaintenanceJob) -> ReviewResult:
        """Advance cursors only; review/adoption remains explicitly injected."""

        return ReviewResult(
            review_cursor=job.last_message_cursor,
            snapshot_cursor=job.last_message_cursor,
        )


__all__ = ["MaintenanceWorker", "ReviewResult"]
