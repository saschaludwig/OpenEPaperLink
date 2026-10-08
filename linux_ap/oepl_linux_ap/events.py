"""WebSocket fan-out and the persistent text log.

The web UI listens on ``/ws`` for JSON messages.  Every ``ws*`` helper of the
ESP firmware (``wsLog``, ``wsErr``, ``wsSendTaginfo``, ``wsSerial`` ...) has a
counterpart here.  Messages are queued and delivered from the event loop so
that worker threads (image generation, serial I/O) can call them safely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


class WebSocketHub:
    """Collects connected WebSocket clients and broadcasts JSON payloads."""

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add_client(self, ws: Any) -> None:
        with self._lock:
            self._clients.add(ws)

    def remove_client(self, ws: Any) -> None:
        with self._lock:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def broadcast(self, payload: dict[str, Any]) -> None:
        """Thread-safe broadcast of one JSON object to all clients."""
        for listener in list(self.listeners):
            try:
                listener(payload)
            except Exception:  # pragma: no cover - listeners must not break the hub
                log.exception("ws listener failed")
        if self._loop is None:
            return
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._loop.create_task(self._send_all(text))
        else:
            self._loop.call_soon_threadsafe(lambda: self._loop.create_task(self._send_all(text)))

    async def _send_all(self, text: str) -> None:
        with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                if ws.closed:
                    self.remove_client(ws)
                    continue
                await ws.send_str(text)
            except Exception:
                self.remove_client(ws)


class Events:
    """High level helpers mirroring the ``ws*`` functions of ``web.cpp``."""

    def __init__(self, hub: WebSocketHub, log_file: Path | None = None):
        self.hub = hub
        self.log_file = log_file

    # -- plain messages ----------------------------------------------------
    def ws_log(self, text: str) -> None:
        log.info("%s", text)
        self.hub.broadcast({"logMsg": text})

    def ws_err(self, text: str) -> None:
        log.error("%s", text)
        self.hub.broadcast({"errMsg": text})

    def ws_serial(self, text: str, color: str | None = None) -> None:
        """Console output of the flasher / OTA pages."""
        log.info("console: %s", text)
        payload: dict[str, Any] = {"console": text}
        if color:
            payload["color"] = color
        self.hub.broadcast(payload)

    def ws_upload_progress(self, percentage: int) -> None:
        self.hub.broadcast({"upload": percentage})

    def ws_taginfo(self, tag_json: dict[str, Any]) -> None:
        self.hub.broadcast({"tags": [tag_json]})

    def ws_apitem(self, apitem: dict[str, Any]) -> None:
        self.hub.broadcast({"apitem": apitem})

    def ws_touch(self, touch: dict[str, Any]) -> None:
        self.hub.broadcast({"touch": touch})

    def ws_sysinfo(self, sys_payload: dict[str, Any]) -> None:
        self.hub.broadcast({"sys": sys_payload})

    # -- persistent log (system.cpp logLine) ------------------------------
    def log_line(self, text: str) -> None:
        log.info("%s", text)
        if self.log_file is None:
            return
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            if self.log_file.exists() and self.log_file.stat().st_size >= 10 * 1024:
                old = self.log_file.with_name("logold.txt")
                if old.exists():
                    old.unlink()
                self.log_file.rename(old)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S ", time.localtime())
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(stamp + text + "\n")
        except OSError as exc:
            log.warning("log_line failed: %s", exc)
