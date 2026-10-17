"""Offline L1 family abstraction, version provenance and fenced L2 publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from nanobot.audit.redaction import AuditRedactor
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.memory.lock import LockLease, acquire_lock, release_lock
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.semantic_evidence import _FRAMEWORK_RE, _SECRET_RE
from nanobot.utils.prompt_templates import render_template

ASSET_LESSON_JOB_ID = "asset-lesson-abstraction"
LESSON_TIMEOUT_SECONDS = 180
LESSON_LEASE_SECONDS = 300
_INSTANCE = re.compile(r"https?://|[/\\]|```|\b[0-9a-f]{24,}\b|\b[\w.-]+\.(?:md|txt|json|py|sh)\b|\b(?:curl|sudo|bash|powershell|exec)\b", re.I)


class LessonDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    error_root_cause: str = Field(min_length=1, max_length=100)
    resolution: str = Field(min_length=1, max_length=100)
    trigger_condition: str = Field(min_length=1, max_length=100)
    applicability_boundary: str = Field(min_length=1, max_length=100)
    supporting_asset_ids: list[str] = Field(min_length=1)


@dataclass(frozen=True)
class LessonBatch:
    family_key: str
    version: str
    rows: tuple[dict[str, Any], ...]
    representative: str
    root_causes: tuple[str, ...]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def lesson_enabled(config: Any) -> bool:
    evo = getattr(config, "evolution", config)
    return (bool(getattr(evo, "asset_layer_enabled", False))
            and bool(getattr(evo, "lesson_abstraction_enabled", False))
            and not bool(getattr(config, "kill_switch", False)))


def register_asset_lesson_job(cron: Any, config: Any) -> bool:
    if not lesson_enabled(config):
        return False
    cron.register_system_job(CronJob(
        id=ASSET_LESSON_JOB_ID, name=ASSET_LESSON_JOB_ID,
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(kind="system_event"),
    ))
    return True


def _family_rows(connection: Any, workspace: str, family: str) -> tuple[dict[str, Any], ...]:
    # Exclude usage counters and timestamps: reuse and duplicate projections do
    # not change the facts sent to abstraction.
    return tuple(dict(row) for row in connection.execute(
        "SELECT asset_id,intent,resource_class,operation_family,outcome,error_class,steps_json,source_trace_ids "
        "FROM memory_assets WHERE workspace=? AND family_key=? AND asset_level='L1' AND status='active' "
        "ORDER BY asset_id", (workspace, family),
    ))


def _batch(connection: Any, workspace: str, family: str, minimum: int) -> LessonBatch | None:
    rows = _family_rows(connection, workspace, family)
    if len(rows) < minimum:
        return None
    if any(row["outcome"] != "success" and row["error_class"] in {None, "", "unknown"} for row in rows):
        return None
    version = _digest(rows)
    if connection.execute(
        "SELECT 1 FROM memory_assets WHERE workspace=? AND family_key=? AND asset_level='L2' "
        "AND status='active' AND lesson_input_version=?", (workspace, family, version),
    ).fetchone():
        return None
    used = {r[0] for r in connection.execute(
        "SELECT origin_id FROM memory_assets WHERE workspace=? AND asset_level='L2' AND origin_table='memory_assets'",
        (workspace,),
    )}
    representative = next((r["asset_id"] for r in reversed(rows) if r["asset_id"] not in used), None)
    if representative is None:
        return None
    errors = sorted({r["error_class"] for r in rows if r["error_class"]})
    causes = tuple(f"已观察到 {error}，具体根因尚无充分证据" for error in errors)
    if not causes:
        causes = ("案例记录了该类操作成功，未提供额外根因证据",)
    return LessonBatch(family, version, rows, representative, causes)


def _input(batch: LessonBatch) -> str:
    counts = Counter((r["intent"], r["resource_class"], r["operation_family"], r["outcome"], r["error_class"]) for r in batch.rows)
    return _json({
        "input_version": batch.version,
        "source_asset_ids": [r["asset_id"] for r in batch.rows],
        "allowed_root_causes": batch.root_causes,
        "observations": [{"intent": key[0], "resource_class": key[1], "operation_family": key[2],
                          "outcome": key[3], "error_class": key[4], "count": count} for key, count in sorted(counts.items(), key=lambda pair: str(pair[0]))],
    })


def _validate(content: str, batch: LessonBatch) -> LessonDraft:
    draft = LessonDraft.model_validate_json(content)
    allowed_ids = {r["asset_id"] for r in batch.rows}
    if draft.error_root_cause not in batch.root_causes or not set(draft.supporting_asset_ids) <= allowed_ids:
        raise ValueError("unsupported lesson evidence")
    fields = draft.model_dump(exclude={"supporting_asset_ids"})
    for text in fields.values():
        if not text.strip() or _SECRET_RE.search(text) or _INSTANCE.search(text) or _FRAMEWORK_RE.search(text):
            raise ValueError("unsafe or instance-specific lesson")
    cleaned, _ = AuditRedactor().redact(fields)
    if cleaned != fields:
        raise ValueError("lesson output required redaction")
    return draft


def _publish(connection: Any, workspace: str, batch: LessonBatch, draft: LessonDraft,
             digests: tuple[str, str], lease: LockLease, trace_id: str | None) -> str:
    connection.execute("BEGIN IMMEDIATE")
    try:
        lock = connection.execute(
            "SELECT owner,generation,lease_until FROM maintenance_lock WHERE workspace=?", (workspace,),
        ).fetchone()
        if (lock is None or lock[0] != lease.owner or lock[1] != lease.generation
                or datetime.fromisoformat(lock[2]) <= datetime.now(UTC)
                or _digest(_family_rows(connection, workspace, batch.family_key)) != batch.version):
            raise ValueError("lesson input or lease changed")
        old = connection.execute(
            "SELECT asset_id FROM memory_assets WHERE workspace=? AND family_key=? AND asset_level='L2' AND status='active' "
            "ORDER BY created_at DESC,asset_id DESC", (workspace, batch.family_key),
        ).fetchall()
        row = next(r for r in batch.rows if r["asset_id"] == batch.representative)
        values = {"asset_id": "lesson:" + uuid.uuid4().hex, "workspace": workspace, "asset_level": "L2",
                  "origin_table": "memory_assets", "origin_id": batch.representative,
                  "replaces_asset_id": old[0][0] if old else None, "family_key": batch.family_key,
                  "intent": row["intent"], "resource_class": row["resource_class"], "operation_family": row["operation_family"],
                  "outcome": row["outcome"], "error_class": row["error_class"],
                  "source_trace_ids": _json(sorted({t for r in batch.rows for t in json.loads(r["source_trace_ids"])})),
                  "error_root_cause": draft.error_root_cause, "resolution": draft.resolution,
                  "trigger_condition": draft.trigger_condition, "applicability_boundary": draft.applicability_boundary,
                  "confidence_source": "inferred", "status": "active", "visibility": "shared",
                  "redaction_level": "normal", "created_at": datetime.now(UTC).isoformat(), "updated_at": datetime.now(UTC).isoformat(),
                  "lesson_input_version": batch.version, "lesson_source_ids_json": _json([r["asset_id"] for r in batch.rows]),
                  "lesson_output_digests_json": _json(digests), "lesson_trace_id": trace_id}
        values["search_text"] = " ".join(str(values[name] or "") for name in (
            "intent", "resource_class", "operation_family", "trigger_condition", "error_class", "error_root_cause", "resolution",
        ))
        connection.execute("INSERT INTO memory_assets(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values()))
        connection.execute("UPDATE memory_assets SET status='archived',updated_at=? WHERE workspace=? AND family_key=? "
                           "AND asset_level='L2' AND status='active' AND asset_id!=?",
                           (values["updated_at"], workspace, batch.family_key, values["asset_id"]))
        connection.commit()
        return values["asset_id"]
    except BaseException:
        connection.rollback()
        raise


async def run_asset_lesson_scan(workspace: str | Path, config: Any, *, provider: Any, model: str,
                                audit_runtime: Any = None) -> dict[str, Any]:
    if not lesson_enabled(config):
        return {"status": "disabled", "created": []}
    from nanobot.memory.lesson_model import abstract_lesson

    root = str(Path(workspace).expanduser().resolve())
    evo = getattr(config, "evolution", config)
    connection = open_maintenance_db(root)
    result: dict[str, Any] = {"status": "completed", "created": [], "failed": 0, "skipped": 0, "traces": []}
    try:
        families = [r[0] for r in connection.execute(
            "SELECT DISTINCT family_key FROM memory_assets WHERE workspace=? AND asset_level='L1' AND status='active' ORDER BY family_key", (root,),
        )]
        for family in families:
            if not lesson_enabled(config):
                result["status"] = "disabled"
                break
            lease = acquire_lock(connection, root, "asset-lesson:" + uuid.uuid4().hex, lease_seconds=LESSON_LEASE_SECONDS)
            if lease is None:
                result["skipped"] += 1
                continue
            try:
                batch = _batch(connection, root, family, int(getattr(evo, "lesson_min_cases", 3)))
                if batch is None:
                    continue
                async with asyncio.timeout(LESSON_TIMEOUT_SECONDS):
                    first, second, trace_id = await abstract_lesson(
                        provider=provider, model=model, system=render_template("memory/asset_lesson.md"),
                        user=_input(batch), workspace=root, input_version=batch.version, audit_runtime=audit_runtime,
                    )
                drafts = (_validate(first, batch), _validate(second, batch))
                if not lesson_enabled(config):
                    result["status"] = "disabled"
                    break
                asset_id = _publish(connection, root, batch, drafts[0], lease=lease,
                                    digests=(_digest(drafts[0].model_dump()), _digest(drafts[1].model_dump())), trace_id=trace_id)
                result["created"].append(asset_id)
                result["traces"].append(trace_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                result["failed"] += 1
                logger.warning("Asset lesson abstraction failed kind={}", type(error).__name__)
            finally:
                release_lock(connection, lease)
        return result
    finally:
        connection.close()
