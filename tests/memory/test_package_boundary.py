"""Verify the initial memory package boundary without creating a database."""

from pathlib import Path


def test_memory_package_boundary_is_importable() -> None:
    import nanobot.memory as memory
    import nanobot.memory.migrations as migrations

    assert memory.__all__ == []
    assert Path(memory.__file__).parent.name == "memory"
    assert Path(migrations.__file__).parent.name == "migrations"


def test_phase_zero_scaffold_does_not_create_runtime_database() -> None:
    import nanobot.memory as memory

    package_dir = Path(memory.__file__).parent
    assert not (package_dir / "memory.sqlite3").exists()
