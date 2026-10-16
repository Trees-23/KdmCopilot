"""Historical SQL-only fixture for pre-Q4 purge/upgrade regression tests."""

from pathlib import Path

from nanobot.memory.db import connect_memory_db
from nanobot.memory.schema import MIGRATIONS_DIR, schema_hash


def open_pre_q4_db(workspace: Path):
    connection = connect_memory_db(workspace)
    if connection.execute("SELECT count(*) FROM sqlite_master WHERE name='schema_meta'").fetchone()[0]:
        return connection
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name[:4])
        if version in {25, 26, 27}:
            continue
        text = path.read_text()
        connection.executescript(text)
        connection.execute(
            "INSERT INTO schema_meta(version,migration_id,app_build,schema_hash,status,applied_at) "
            "VALUES(?,?,?,?,'applied','2026-10-16T00:00:00+00:00')",
            (version, path.stem, "pre-q4-fixture", schema_hash(text)),
        )
        connection.commit()
    return connection
