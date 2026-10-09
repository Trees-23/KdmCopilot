"""Read-only Skill catalog tools and candidate proposal boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.memory.maintenance import open_maintenance_db


class _SkillCatalogBase(Tool):
    _scopes = {"core", "subagent"}

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.loader = SkillsLoader(self.workspace)

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ctx.workspace)


@tool_parameters({
    "type": "object",
    "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
    "required": ["query"],
})
class SkillCatalogSearchTool(_SkillCatalogBase):
    @property
    def name(self) -> str:
        return "skill_catalog_search"

    @property
    def description(self) -> str:
        return "Search available Skill metadata without loading full Skill content."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, query: str, limit: int = 10) -> str:
        needle = query.casefold().strip()
        rows = []
        for entry in self.loader.list_skills(filter_unavailable=False):
            description = self.loader.get_skill_metadata(entry["name"]) or {}
            text = f"{entry['name']} {description.get('description', '')}".casefold()
            if needle and needle not in text:
                continue
            rows.append({"name": entry["name"], "description": description.get("description", ""), "source": entry["source"]})
        return json.dumps(rows[:limit], ensure_ascii=False, sort_keys=True)


@tool_parameters({
    "type": "object",
    "properties": {"name": {"type": "string", "minLength": 1}},
    "required": ["name"],
})
class SkillReadTool(_SkillCatalogBase):
    @property
    def name(self) -> str:
        return "skill_read"

    @property
    def description(self) -> str:
        return "Read one available Skill markdown document."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, name: str) -> str:
        content = self.loader.load_skill(name)
        if content is None:
            return ToolResult.error(f"Error: Skill '{name}' not found", error_code="skill_not_found")
        return content


@tool_parameters({
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "content": {"type": "string", "minLength": 1},
        "source_case_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "content"],
})
class SkillProposeTool(_SkillCatalogBase):
    @property
    def name(self) -> str:
        return "skill_propose"

    @property
    def description(self) -> str:
        return "Create a staging Skill candidate; never changes the current Skill revision."

    async def execute(self, name: str, content: str, source_case_ids: list[str] | None = None) -> str:
        skill_id = f"skill:{name}"
        revision_id = str(uuid4())
        content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        connection = open_maintenance_db(self.workspace)
        try:
            timestamp = datetime.now(UTC).isoformat(timespec="seconds")
            connection.execute(
                """INSERT INTO skills(skill_id,namespace,name,source_kind,current_version,status,description,
                   tool_policy_json,references_json,created_at,updated_at)
                   VALUES(?,?,?,'workspace',NULL,'staging','{}','{}','[]',?,?)
                   ON CONFLICT(namespace,name) DO NOTHING""",
                (skill_id, "workspace", name, timestamp, timestamp),
            )
            connection.execute(
                """INSERT INTO skill_revisions
                   (revision_id,skill_id,skill_version,content_hash,content,source_case_ids_json,
                    author_actor,status,created_at)
                   VALUES(?,?,?,?,?,?,?,'staging',?)""",
                (revision_id, skill_id, "candidate", content_hash, content,
                 json.dumps(source_case_ids or [], ensure_ascii=False), "agent", timestamp),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return json.dumps({"skill_id": skill_id, "revision_id": revision_id, "status": "staging"}, sort_keys=True)


__all__ = ["SkillCatalogSearchTool", "SkillProposeTool", "SkillReadTool"]
