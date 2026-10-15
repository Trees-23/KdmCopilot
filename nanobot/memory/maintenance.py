"""Workspace database connection used by Skill proposal tools."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from nanobot.memory.db import connect_memory_db
from nanobot.memory.migrations.runner import apply_migrations


def open_maintenance_db(workspace: str | Path) -> sqlite3.Connection:
    connection = connect_memory_db(workspace)
    apply_migrations(connection)
    return connection


__all__ = ["open_maintenance_db"]
