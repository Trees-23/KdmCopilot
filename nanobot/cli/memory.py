"""Explicit offline memory administration commands."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer
from rich.console import Console

from nanobot.memory.legacy_purge import PurgeError, preview_legacy_purge, purge_legacy_data
from nanobot.memory.maintenance_purge import preview_maintenance_purge, purge_maintenance_data


def create_memory_app(*, console: Console) -> typer.Typer:
    memory_app = typer.Typer(help="离线记忆数据库管理", no_args_is_help=True)

    @memory_app.command("purge-legacy")
    def purge_legacy(
        database: Path = typer.Option(..., "--database", help="已有的 memory.sqlite3 完整路径"),
        dry_run: bool = typer.Option(False, "--dry-run", help="只读预览（默认行为）"),
        execute: bool = typer.Option(False, "--execute", help="明确授权后执行一次性 P6 清理"),
        confirm_plan: str = typer.Option("", "--confirm-plan", help="停写后 dry-run 的 plan_digest"),
        writers_stopped: bool = typer.Option(False, "--writers-stopped", help="已停止 Gateway、worker 和其他写入进程"),
    ) -> None:
        if dry_run and execute:
            raise typer.BadParameter("--dry-run 与 --execute 不能同时使用")
        try:
            result = purge_legacy_data(database, expected_plan=confirm_plan, writers_stopped=writers_stopped) if execute else preview_legacy_purge(database)
        except (PurgeError, OSError, sqlite3.Error) as exc:
            # SQL errors/triggers may contain user content; report no raw SQL text.
            message = str(exc) if isinstance(exc, PurgeError) else type(exc).__name__
            console.print(f"清理未执行或已回滚：{message}", markup=False)
            raise typer.Exit(1) from None
        console.print_json(json.dumps(result, ensure_ascii=False))

    @memory_app.command("purge-maintenance")
    def purge_maintenance(
        database: Path = typer.Option(..., "--database", help="已有的 memory.sqlite3 完整路径"),
        dry_run: bool = typer.Option(False, "--dry-run", help="只读预览（默认行为）"),
        execute: bool = typer.Option(False, "--execute", help="单独授权后执行 C-P6 维护表清理"),
        confirm_plan: str = typer.Option("", "--confirm-plan", help="停写后 dry-run 的 plan_digest"),
        writers_stopped: bool = typer.Option(False, "--writers-stopped", help="已停止所有数据库使用者"),
    ) -> None:
        if dry_run and execute:
            raise typer.BadParameter("--dry-run 与 --execute 不能同时使用")
        try:
            result = purge_maintenance_data(database, expected_plan=confirm_plan, writers_stopped=writers_stopped) if execute else preview_maintenance_purge(database)
        except (PurgeError, OSError, sqlite3.Error) as exc:
            message = str(exc) if isinstance(exc, PurgeError) else type(exc).__name__
            console.print(f"维护清理未执行或已回滚：{message}", markup=False)
            raise typer.Exit(1) from None
        console.print_json(json.dumps(result, ensure_ascii=False))

    return memory_app
