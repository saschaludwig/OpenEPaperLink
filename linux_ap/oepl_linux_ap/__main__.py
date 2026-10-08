"""Service entry point: ``python -m oepl_linux_ap`` or ``oepl-linux-ap``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

from aiohttp import web

from . import __version__
from .config import Settings, load_settings
from .content.manager import ContentManager
from .flasher import C6Flasher
from .newproto import NewProto
from .protocol import RUNSTATUS_INIT, RUNSTATUS_RUN, RUNSTATUS_STOP
from .serialap import SerialAP
from .state import AppContext
from .udp import UDPSync
from .web import create_app, sysinfo_task

log = logging.getLogger("oepl_linux_ap")


def _prepare_storage(ctx: AppContext) -> None:
    settings = ctx.settings
    for folder in (settings.data_path, settings.current_dir, settings.data_path / "temp",
                   settings.data_path / "fonts"):
        folder.mkdir(parents=True, exist_ok=True)
    ctx.db.tagtypes.seed()
    if not ctx.db.load():
        backup = settings.tagdb_file.with_suffix(".json.bak")
        if backup.exists():
            ctx.db.load(backup)
    ctx.db.cleanup_current()


async def _periodic_save(ctx: AppContext, interval: float = 300.0) -> None:
    while True:
        await asyncio.sleep(interval)
        if ctx.db.config.run_status != RUNSTATUS_STOP:
            try:
                ctx.db.save()
            except Exception:
                log.exception("saving tag database failed")


async def _delayed_start(ctx: AppContext, delay: float = 30.0) -> None:
    """Content generation starts after a grace period (``delayedStart``)."""
    await asyncio.sleep(delay)
    if ctx.db.config.run_status != RUNSTATUS_RUN:
        ctx.events.ws_log("starting content generation")
        ctx.db.config.run_status = RUNSTATUS_RUN


async def run(settings: Settings) -> int:
    ctx = AppContext.create(settings)
    loop = asyncio.get_running_loop()
    ctx.hub.bind_loop(loop)

    os.environ["TZ"] = ctx.db.config.timezone
    time.tzset()

    _prepare_storage(ctx)
    ctx.events.log_line(f"Startup oepl-linux-ap {__version__}")
    ctx.db.config.run_status = RUNSTATUS_INIT

    stop_event = asyncio.Event()

    def request_shutdown() -> None:
        loop.call_soon_threadsafe(stop_event.set)

    ctx.request_shutdown = request_shutdown
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    # Subsystems
    ctx.serial = SerialAP(ctx)
    ctx.proto = NewProto(ctx)
    ctx.content = ContentManager(ctx)
    ctx.flasher = C6Flasher(ctx)
    if settings.enable_udp_sync:
        ctx.udp = UDPSync(ctx)

    app = create_app(ctx)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.http_host, settings.http_port)
    await site.start()
    log.info("HTTP server listening on %s:%d", settings.http_host, settings.http_port)

    tasks = [
        loop.create_task(sysinfo_task(ctx), name="sysinfo"),
        loop.create_task(_periodic_save(ctx), name="savedb"),
        loop.create_task(_delayed_start(ctx), name="delayedstart"),
        loop.create_task(ctx.serial.run(), name="serial"),
        loop.create_task(ctx.content.run(), name="content"),
    ]
    if ctx.udp is not None:
        tasks.append(loop.create_task(ctx.udp.run(), name="udp"))

    await stop_event.wait()
    log.info("shutting down")
    ctx.db.config.run_status = RUNSTATUS_STOP
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        ctx.db.save()
    except Exception:
        log.exception("saving tag database on shutdown failed")
    if ctx.serial is not None:
        await ctx.serial.close()
    await runner.cleanup()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenEPaperLink access point for Linux boards")
    parser.add_argument("--config", help="JSON file with machine settings", default=None)
    parser.add_argument("--data-dir", help="override data directory", default=None)
    parser.add_argument("--port", type=int, help="HTTP port", default=None)
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    if args.data_dir:
        settings.data_dir = args.data_dir
    if args.port:
        settings.http_port = args.port
    if args.log_level:
        settings.log_level = args.log_level

    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
