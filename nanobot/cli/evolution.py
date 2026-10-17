"""Offline worker entrypoint; no Gateway or channel listener is started."""

import asyncio
from pathlib import Path

import typer

from nanobot.memory.evolution_worker import run_worker


def evolution_worker(
    config: Path = typer.Option(Path.home() / ".nanobot/config.json", "--config", "-c"),
    gateway_health_url: str = typer.Option("http://nanobot-gateway:18790/health", "--gateway-health-url"),
    once: bool = typer.Option(False, "--once", help="只处理一个任务（或一次空轮询）"),
) -> None:
    """运行与固定 Gateway 同镜像的自进化离线执行者。"""
    try:
        asyncio.run(run_worker(config, health_url=gateway_health_url, once=once))
    except KeyboardInterrupt:
        return
