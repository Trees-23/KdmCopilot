"""Same-image offline execution, separate from Gateway channels and scheduling."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable
from urllib.request import ProxyHandler, build_opener

from nanobot.memory.continuous import load_state
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.queue_state import reap_stale_evaluations
from nanobot.memory.work_queue import claim_work, finish_work, reap_work, renew_work

WORK_TIMEOUT_SECONDS = 3600
POLL_SECONDS = 5


class WorkerBuildMismatchError(RuntimeError):
    """Execution is forbidden when the Gateway and worker versions differ."""


def gateway_build(health_url: str) -> str:
    # Internal service health traffic must not inherit a remote HTTP proxy.
    with build_opener(ProxyHandler({})).open(health_url, timeout=5) as response:
        value = json.load(response)
    if value.get("status") != "ok":
        raise WorkerBuildMismatchError("gateway_not_ready")
    return str(value.get("build", {}).get("ref", "unknown"))


def verify_worker_build(runtime_root: Path, own_ref: str, gateway_ref: str) -> None:
    """Record only build identity and a safe status; never config or credentials."""
    directory = runtime_root / "evolution-worker"
    directory.mkdir(parents=True, exist_ok=True)
    matched = own_ref != "unknown" and own_ref == gateway_ref
    record = {"build": {"ref": own_ref}, "gateway_ref": gateway_ref,
              "status": "ready" if matched else "rejected", "reason": None if matched else "build_mismatch"}
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as temporary:
        json.dump(record, temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_path = Path(temporary.name)
    temp_path.replace(directory / "build.json")
    if not matched:
        raise WorkerBuildMismatchError("worker_gateway_build_mismatch")


async def execute_work(kind: str, config: Any) -> dict:
    """Initialize the same provider/tools/runtime as Gateway, without channels."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.paths import get_audit_dir
    from nanobot.memory.asset_lesson import run_asset_lesson_scan
    from nanobot.memory.phase6_runtime import build_overlay_pipeline
    from nanobot.memory.phase6_trigger import run_phase6_review_scan_async
    from nanobot.memory.proposal_notifier import ProposalNotifier

    bus = MessageBus()
    # ``from_config`` supplies the configured MCP map itself. Passing a second
    # value here would create duplicate keyword arguments and abort the worker.
    worker_config = copy.deepcopy(config)
    worker_config.tools.mcp_servers = {}
    agent = AgentLoop.from_config(worker_config, bus=bus, publish_callbacks=None)
    notifier = ProposalNotifier(str(config.workspace_path), config.phase6, bus)

    def notify(proposal_id: str) -> None:
        for group in config.phase6.evolution.notification_groups:
            notifier.enqueue(proposal_id, group_openid=group)

    try:
        await agent.audit_runtime.ensure_started()
        if kind == "lesson":
            return await run_asset_lesson_scan(
                config.workspace_path, config.phase6, provider=agent.provider, model=agent.model,
                audit_runtime=agent.audit_runtime,
            )
        if kind != "review":
            raise ValueError("unknown worker kind")
        result = await run_phase6_review_scan_async(
            config.workspace_path, config.phase6, audit_root=get_audit_dir(config.audit.path),
            overlay_pipeline_factory=lambda connection, workspace, phase6_config: build_overlay_pipeline(
                connection, workspace, phase6_config, notifier=notifier,
            ),
            notify_proposal=notify, notify_error=notifier.enqueue_scan_failure, active_agent=agent,
        )
        return {"status": str(getattr(result, "status", "completed")),
                "proposal_count": len(getattr(result, "proposal_ids", ()))}
    finally:
        await agent.close_mcp()


async def worker_tick(config: Any, *, owner: str, execute: Callable = execute_work) -> dict:
    """Recover expired work/candidate leases even without a fresh Cron trigger."""
    workspace = str(config.workspace_path.resolve())
    connection = open_maintenance_db(workspace)
    lease = None
    try:
        if not config.phase6.enabled or config.phase6.kill_switch:
            return {"status": "disabled"}
        reap_work(connection, workspace)
        reap_stale_evaluations(connection, workspace=workspace,
                               max_attempts=config.phase6.evolution.evaluation_max_attempts)
        if load_state(workspace).paused:
            return {"status": "paused"}
        lease = claim_work(connection, workspace, owner)
        if lease is None:
            return {"status": "idle"}
    finally:
        connection.close()

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(20)
            connection = open_maintenance_db(workspace)
            try:
                if not renew_work(connection, lease):
                    raise RuntimeError("work_lease_lost")
            finally:
                connection.close()

    task = asyncio.create_task(execute(lease.kind, config))
    pulse = asyncio.create_task(heartbeat())
    error_code = None
    result = {}
    cancelled = False
    try:
        async with asyncio.timeout(WORK_TIMEOUT_SECONDS):
            done, _ = await asyncio.wait((task, pulse), return_when=asyncio.FIRST_COMPLETED)
            if pulse in done:
                pulse.result()
                raise RuntimeError("heartbeat_stopped")
            result = task.result()
    except asyncio.CancelledError:
        error_code, cancelled = "worker_stopped", True
    except TimeoutError:
        error_code = "work_timeout"
    except Exception as exc:
        error_code = type(exc).__name__
    finally:
        task.cancel()
        pulse.cancel()
        await asyncio.gather(task, pulse, return_exceptions=True)
        connection = open_maintenance_db(workspace)
        try:
            acknowledged = finish_work(connection, lease, error_code=error_code, result=result)
        finally:
            connection.close()
    if cancelled:
        raise asyncio.CancelledError
    return {"status": "acknowledged" if acknowledged else "lease_lost",
            "work_id": lease.work_id, "error_code": error_code}


async def run_worker(config_path: Path, *, health_url: str, once: bool = False) -> None:
    from nanobot.config.loader import load_config

    own_ref = os.environ.get("NANOBOT_BUILD_REF", "unknown")
    owner = "evolution-worker:" + uuid.uuid4().hex
    # Compose starts Gateway first; give its normal health server time to bind.
    for attempt in range(12):
        try:
            gateway_ref = await asyncio.to_thread(gateway_build, health_url)
            break
        except (OSError, ValueError):
            if attempt == 11:
                raise WorkerBuildMismatchError("gateway_health_unavailable") from None
            await asyncio.sleep(5)
    verify_worker_build(config_path.resolve().parent, own_ref, gateway_ref)
    while True:
        # A rolling Gateway update must stop an old worker before the next claim.
        gateway_ref = await asyncio.to_thread(gateway_build, health_url)
        verify_worker_build(config_path.resolve().parent, own_ref, gateway_ref)
        config = load_config(config_path)
        await worker_tick(config, owner=owner)
        if once:
            return
        await asyncio.sleep(POLL_SECONDS)
