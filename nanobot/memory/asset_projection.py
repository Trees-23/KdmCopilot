"""Single-direction, bounded L1 projection from persisted task and step facts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from nanobot.audit.redaction import AuditRedactor, RedactionError
from nanobot.memory.asset_taxonomy import error_class, operation_family, tool_category
from nanobot.memory.semantic_evidence import _FRAMEWORK_RE, _SECRET_RE
from nanobot.memory.task_outcome import TASK_OUTCOMES

_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,79}$")
_IDENTITY = re.compile(r"(?i)\b(?:openid|user[_ -]?id|message[_ -]?id|bootstrap[_ -]?secret|authorization)\s*[:=]\s*\S+|\b[0-9a-f]{32}\b")
_PAYLOAD = re.compile(r"```|-----BEGIN .*PRIVATE KEY-----|(?:^|\n)\s*(?:curl|sudo|bash|powershell|rm\s+-)\b", re.I)
_LEVELS = {"R0": "normal", "R1": "normal", "R2": "abstract", "R3": "maximal", "R4": "maximal"}


def _label(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and _LABEL.fullmatch(value) else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def project_task_asset(connection: Any, task_id: str, *, source_group: str | None = None) -> str | None:
    """Project one freshly persisted four-state outcome; never infer from legacy 'completed'."""
    task = connection.execute("SELECT * FROM evolution_task_evidence WHERE task_id=?", (task_id,)).fetchone()
    if task is None or task["actual_outcome"] not in TASK_OUTCOMES or not task["goal"]:
        return None
    goal = str(task["goal"])
    if _FRAMEWORK_RE.search(goal) or _IDENTITY.search(goal) or _PAYLOAD.search(goal) or _SECRET_RE.search(goal):
        return None
    facts = connection.execute(
        "SELECT * FROM evolution_step_evidence WHERE task_id=? ORDER BY sequence_no", (task_id,),
    ).fetchall()
    if not facts or any(step["redaction_status"] != "safe" for step in facts):
        return None
    steps = []
    for fact in facts:
        category = tool_category(fact["tool_name"])
        family = operation_family(fact["tool_name"], fact["operation_kind"])
        risk = fact["risk_level"]
        if risk not in _LEVELS:
            return None
        steps.append({
            "seq": fact["sequence_no"], "operation_family": family,
            "resource_class": fact["resource_class"], "tool_category": category,
            "status": fact["status"], "risk_level": risk, "redaction_level": _LEVELS[risk],
            # Source summaries can contain concrete paths. Assets keep only category summaries.
            "input_summary": f"{family} / {fact['resource_class']}" if risk in {"R0", "R1"} else None,
            "output_summary": None,
            "error_code": _label(fact["error_code"]), "error_type": _label(fact["error_type"]),
            "error_source": _label(fact["error_source"]), "error_class": error_class(fact["error_code"]),
            "retryability": fact["retryability"], "verification_kind": _label(fact["verification_kind"]),
        })
    worst = max(steps, key=lambda step: step["risk_level"])
    failed = [step for step in steps if step["status"] in {"error", "blocked", "timeout"}]
    primary = failed[-1] if failed else worst
    family, resource, failure = primary["operation_family"], primary["resource_class"], primary["error_class"]
    if worst["risk_level"] in {"R2", "R3", "R4"}:
        # A high-risk goal must not preserve a quoted command, payload or file body.
        goal = f"{task['intent'] or '一般任务'}：{family}（{resource}）"
    try:
        cleaned, _ = AuditRedactor().redact({"goal": goal})
    except RedactionError:
        return None
    goal = str(cleaned["goal"]).strip()[:360]
    if not goal or _IDENTITY.search(goal) or _PAYLOAD.search(goal) or _SECRET_RE.search(goal):
        return None
    outcome = task["actual_outcome"]
    discriminator = resource if outcome == "success" else failure or ""
    family_key = "|".join((task["workspace"], task["intent"] or "一般任务", family, discriminator))
    trigger = f"{task['intent'] or '一般任务'} / {family} / {failure or resource}"
    search = " ".join((goal, str(task["intent"] or ""), resource, family, str(failure or ""), trigger))
    asset_id = "asset:" + hashlib.sha256(f"{task['workspace']}\0{task_id}".encode()).hexdigest()[:32]
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    connection.execute(
        """INSERT INTO memory_assets(asset_id,workspace,asset_level,source_group,source_session,source_trace_ids,
           origin_table,origin_id,task_goal,intent,resource_class,operation_family,trigger_condition,family_key,
           search_text,steps_json,step_count,tools_used,outcome,error_class,redaction_level,visibility,status,
           created_at,updated_at)
           VALUES(?,?,'L1',?,?,?,'evolution_task_evidence',?,?,?,?,?,?,?,?,?,?,?,?,?,?,'shared','active',?,?)
           ON CONFLICT(workspace,origin_table,origin_id) DO UPDATE SET
           source_trace_ids=excluded.source_trace_ids,task_goal=excluded.task_goal,intent=excluded.intent,
           resource_class=excluded.resource_class,operation_family=excluded.operation_family,
           trigger_condition=excluded.trigger_condition,family_key=excluded.family_key,search_text=excluded.search_text,
           steps_json=excluded.steps_json,step_count=excluded.step_count,tools_used=excluded.tools_used,
           outcome=excluded.outcome,error_class=excluded.error_class,redaction_level=excluded.redaction_level,
           updated_at=excluded.updated_at""",
        (asset_id, task["workspace"], source_group, task["session_key"], task["source_trace_ids_json"], task_id,
         goal, task["intent"], resource, family, trigger, family_key, search, _json(steps), len(steps),
         _json(sorted({step["tool_category"] for step in steps})), outcome, failure, _LEVELS[worst["risk_level"]], now, now),
    )
    connection.commit()
    return asset_id
