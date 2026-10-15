"""The retained database connection still persists Skill proposals."""

import json

from nanobot.agent.tools.skill_catalog import SkillProposeTool
from nanobot.memory.maintenance import open_maintenance_db


async def test_skill_proposal_persists_through_retained_database_connection(tmp_path):
    result = json.loads(await SkillProposeTool(tmp_path).execute(
        name="retained-connection",
        content="# Retained connection\n\nInspect a workspace document.",
    ))

    connection = open_maintenance_db(tmp_path)
    try:
        row = connection.execute(
            "SELECT s.status,s.current_revision_id,r.content,r.status "
            "FROM skills s JOIN skill_revisions r ON r.skill_id=s.skill_id "
            "WHERE s.skill_id=? AND r.revision_id=?",
            (result["skill_id"], result["revision_id"]),
        ).fetchone()
        assert tuple(row) == (
            "staging", None, "# Retained connection\n\nInspect a workspace document.", "staging",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM maintenance_jobs").fetchone()[0] == 0
    finally:
        connection.close()
