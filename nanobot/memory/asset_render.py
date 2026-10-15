"""Read-only L1/L2 context rendering with a separate complete-block token budget."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from nanobot.utils.helpers import estimate_message_tokens

if TYPE_CHECKING:
    from nanobot.memory.asset_retriever import AssetHit

ASSET_TOKEN_BUDGET = 600
_HEADER = "# Relevant Past Cases (read-only)"
_OUTCOMES = {"success": "成功", "failure": "失败", "partial": "部分完成", "recovered": "失败后已纠正"}


@dataclass(frozen=True, slots=True)
class RenderedAssets:
    text: str = ""
    hits: tuple[AssetHit, ...] = ()
    tokens: int = 0


def render_asset_body(row: Mapping[str, Any]) -> tuple[str, str]:
    """Return title/body without transport identities or executable instance payloads."""
    outcome = _OUTCOMES.get(str(row["outcome"]), "未知")
    if row["asset_level"] == "L2":
        confidence = "人工确认" if row["confidence_source"] == "confirmed" else "离线推断"
        return str(row["trigger_condition"] or "经验"), "\n".join((
            f"适用：{row['trigger_condition']}", f"根因：{row['error_root_cause']}",
            f"做法：{row['resolution']}", f"边界：{row['applicability_boundary']}",
            f"（置信：{confidence}）",
        ))
    categories = json.loads(str(row["tools_used"] or "[]"))
    if not isinstance(categories, list):
        raise ValueError("asset tools must be an array")
    category = "、".join(str(item) for item in categories)
    if row["redaction_level"] == "maximal":
        return "操作记录", f"操作类别：{category}\n结果：{outcome}"
    operation, resource = str(row["operation_family"] or "unknown"), str(row["resource_class"] or "unknown")
    if row["redaction_level"] == "abstract":
        return f"{operation} / {resource}", f"操作类别：{operation}\n资源类别：{resource}\n结果：{outcome}"
    steps = json.loads(str(row["steps_json"] or "[]"))
    if not isinstance(steps, list):
        raise ValueError("asset steps must be an array")
    process = []
    for step in steps[:6]:
        if not isinstance(step, dict):
            raise ValueError("asset steps must be objects")
        label = f"{step.get('operation_family', operation)} / {step.get('resource_class', resource)}（{step.get('status', 'unknown')}）"
        if step.get("error_class"):
            label += f"；错误类别：{step['error_class']}"
        process.append(label)
    return str(row["task_goal"] or "任务记录"), "\n".join((
        f"目标：{row['task_goal']}",
        f"过程：{'；'.join(process) or operation + ' / ' + resource}（共{int(row['step_count'])} 步）",
        f"结果：{outcome}",
    ))


def fit_asset_budget(hits: Iterable[AssetHit], token_budget: int = ASSET_TOKEN_BUDGET) -> RenderedAssets:
    if not 1 <= token_budget <= ASSET_TOKEN_BUDGET:
        raise ValueError("asset token budget must be between 1 and 600")
    ordered = sorted(hits, key=lambda hit: (0 if hit.asset_level == "L2" else 1, -hit.score, hit.asset_id))
    selected, blocks = [], []
    counts = {"L2": 0, "L1": 0}
    text = ""
    for hit in ordered:
        if hit.asset_level not in counts or counts[hit.asset_level] >= (2 if hit.asset_level == "L2" else 3):
            continue
        label = "L2 经验" if hit.asset_level == "L2" else "L1 案例"
        block = f"## [{label}] {hit.title[:160]}\n{hit.body}"
        candidate = _HEADER + "\n\n" + "\n\n".join((*blocks, block))
        tokens = estimate_message_tokens({"role": "system", "content": candidate})
        if tokens > token_budget:
            continue
        blocks.append(block)
        selected.append(hit)
        counts[hit.asset_level] += 1
        text = candidate
    return RenderedAssets(text, tuple(selected), estimate_message_tokens({"role": "system", "content": text}) if text else 0)
