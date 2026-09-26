"""SQLite connection and workspace path handling for memory storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TypeAlias

MEMORY_DIRNAME = ".nanobot"
MEMORY_DB_FILENAME = "memory.sqlite3"
SQLITE_BUSY_TIMEOUT_MS = 5000

WorkspacePath: TypeAlias = str | Path


class MemoryWorkspaceError(ValueError):
    """Raised when a memory workspace cannot be safely resolved."""


def resolve_memory_db_path(workspace: WorkspacePath) -> Path:
    """Resolve and create the workspace-local memory database parent directory."""

    if workspace is None or not str(workspace).strip():
        raise MemoryWorkspaceError("workspace path must not be empty")
    path = Path(workspace).expanduser()
    if not path.exists() or not path.is_dir():
        raise MemoryWorkspaceError("workspace must be an existing directory")
    workspace_path = path.resolve()
    memory_dir = workspace_path / MEMORY_DIRNAME
    memory_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        memory_dir.chmod(0o700)
    except OSError:
        # Windows and restricted filesystems may not support chmod; creation still
        # remains safe because no existing files are replaced.
        pass
    return memory_dir / MEMORY_DB_FILENAME


def _configure_connection(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def connect_memory_db(workspace: WorkspacePath) -> sqlite3.Connection:
    """Open a workspace memory database with safety pragmas on every connection."""

    return connect_memory_db_path(resolve_memory_db_path(workspace))


def connect_memory_db_path(path: str | Path) -> sqlite3.Connection:
    """Open an explicit database path, primarily for temporary/in-memory tests."""

    if str(path) == ":memory:":
        return _configure_connection(sqlite3.connect(":memory:", timeout=5.0))
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _configure_connection(sqlite3.connect(db_path, timeout=5.0))


def foreign_keys_enabled(connection: sqlite3.Connection) -> bool:
    """Return whether the supplied connection has foreign keys enabled."""

    return connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
