"""Central, payload-free taxonomy for L1/L2 evidence and aggregation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

RESOURCE_CLASSES = frozenset({"workspace-doc", "workspace-dir", "external-url", "unknown"})
TOOL_CATEGORIES = {
    **dict.fromkeys(("read_file", "list_dir", "find_files", "grep"), "filesystem-read"),
    **dict.fromkeys(("write_file", "edit_file", "apply_patch"), "filesystem-write"),
    **dict.fromkeys(("skill_read", "skill_catalog_search"), "skill-read"),
    **dict.fromkeys(("exec", "write_stdin", "run_cli_app"), "process-exec"),
    **dict.fromkeys(("web_fetch", "web_search"), "external-read"),
    "message": "external-message", "cron": "scheduler", "generate_image": "artifact-generation",
    **dict.fromkeys(("spawn", "await_subagents"), "subagent"),
    **dict.fromkeys(("create_goal", "update_goal"), "goal-management"),
}
OPERATION_FAMILIES = {
    "filesystem-read": "read-only-inspect", "skill-read": "read-only-inspect",
    "filesystem-write": "controlled-change", "process-exec": "process-execute",
    "external-read": "external-inspect", "external-message": "external-communication",
    "scheduler": "schedule-change", "artifact-generation": "artifact-generation",
    "subagent": "delegation", "goal-management": "goal-management",
}
ERROR_CLASSES = {
    **dict.fromkeys(("file_not_found", "resource_not_found", "skill_not_found"), "resource-not-found"),
    **dict.fromkeys(("permission", "permission_denied", "access_denied", "forbidden"), "permission-denied"),
    **dict.fromkeys(("timeout", "web_search_timeout", "web_fetch_timeout", "tool_timeout"), "timeout"),
    **dict.fromkeys(("invalid_tool_arguments", "invalid_arguments", "validation_error"), "invalid-arguments"),
    **dict.fromkeys(("policy_blocked", "workspace_violation", "ssrf_blocked"), "policy-blocked"),
    **dict.fromkeys(("rate_limit", "rate_limited"), "rate-limited"),
}
_OPERATION_TO_FAMILY = {
    **dict.fromkeys(("file_read", "text_search", "directory_list", "file_search", "skill_search", "skill_read"), "read-only-inspect"),
    **dict.fromkeys(("file_write", "file_edit", "file_patch"), "controlled-change"),
    **dict.fromkeys(("process_execute", "process_continue"), "process-execute"),
}


def tool_category(name: str) -> str:
    return TOOL_CATEGORIES.get(name, "other-operation")


def operation_family(tool_name: str, operation_kind: str = "") -> str:
    return OPERATION_FAMILIES.get(tool_category(tool_name), _OPERATION_TO_FAMILY.get(operation_kind, "other-operation"))


def error_class(error_code: str | None) -> str | None:
    # Unknown retryability and old coarse failure_family are never error classes.
    return ERROR_CLASSES.get(str(error_code or "").casefold())


def resource_class(tool_name: str, tool: Any, params: Any) -> str:
    """Classify resolved facts without letting diagnostic failures change tool execution."""
    if not isinstance(params, dict):
        return "unknown"
    try:
        if tool_name == "web_fetch":
            parsed = urlparse(str(params.get("url") or ""))
            return "external-url" if parsed.scheme in {"http", "https"} and parsed.hostname else "unknown"
        if tool_name not in {"read_file", "list_dir", "find_files", "grep", "write_file", "edit_file"}:
            return "unknown"
        display_workspace = getattr(tool, "_display_workspace", None)
        workspace = display_workspace() if callable(display_workspace) else getattr(tool, "_workspace", None)
        if workspace is None:
            return "unknown"
        raw = params.get("path", "." if tool_name in {"list_dir", "find_files", "grep"} else None)
        if not isinstance(raw, str) or not raw.strip():
            return "unknown"
        resolver = getattr(tool, "_resolve_write" if tool_name in {"write_file", "edit_file"} else "_resolve_read", None)
        resolver = resolver or getattr(tool, "_resolve", None)
        if not callable(resolver):
            return "unknown"
        target = Path(resolver(raw)).resolve()
        target.relative_to(Path(workspace).resolve())
        if tool_name == "list_dir" or target.is_dir():
            return "workspace-dir"
        if target.suffix.casefold() in {".md", ".txt"}:
            return "workspace-doc"
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    return "unknown"
