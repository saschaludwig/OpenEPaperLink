"""Flashing the ESP32-C6 radio co-processor (``C6firmwareUpdateTask``).

The ESP firmware embeds a serial flasher; here ``esptool`` does the work over
the second UART while the GPIO lines put the C6 into download mode.  Log
lines are pushed to the browser through the ``console``/serial WebSocket
channel exactly like the firmware does, including the ``Progress: N%``,
``[reboot]`` and ``[autoreboot]`` markers ``ota.js`` listens for.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from . import leds
from . import protocol as P
from .state import ap_type_name

if TYPE_CHECKING:  # pragma: no cover
    from .state import AppContext

log = logging.getLogger(__name__)

MANIFEST_NAME = "firmware_C6.json"
_PROGRESS_RE = re.compile(r"\((\d{1,3})\s*%\)")


class C6Flasher:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.events = ctx.events
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def staging_dir(self) -> Path:
        return self.settings.data_path / "c6_staging"

    def start_update(self, url: str) -> bool:
        """Kick off the update task; returns False when one is already running."""
        if self.running:
            self.events.ws_serial("A C6 update is already running")
            return False
        self._task = asyncio.get_running_loop().create_task(self._update_task(url.strip()), name="c6-flash")
        return True

    # ------------------------------------------------------------------
    async def _update_task(self, url: str) -> None:
        serial = self.ctx.serial
        self.events.ws_serial("Stopping AP service")
        previous_run_status = self.ctx.db.config.run_status
        self.ctx.db.config.run_status = P.RUNSTATUS_STOP
        if serial is not None:
            await serial.suspend_for_flashing()
        self.events.ws_serial("C6 flash starting")

        result = False
        try:
            result = await self._flash(url)
        except Exception as exc:  # noqa: BLE001
            log.exception("C6 flash failed")
            self.events.ws_serial(f"Flash error: {exc}")
        self.events.ws_serial("C6 flash end")

        if result and serial is not None:
            self.events.ws_serial("Finishing config...")
            await asyncio.sleep(3.0)
            self.events.ws_serial("starting monitor")
            self.ctx.ap_info.version = 0
            self.events.ws_serial("bringing AP online")
            if await serial.resume_after_flashing():
                self.ctx.db.config.run_status = P.RUNSTATUS_RUN
            await asyncio.sleep(0.5)
            if self.ctx.ap_info.version == 0:
                result = False
        elif serial is not None:
            await serial.resume_after_flashing()
            self.ctx.db.config.run_status = previous_run_status

        if self.ctx.leds is not None:
            # flasher.cpp: fade to green on success, red on failure
            self.ctx.leds.add_fade_color(leds.GREEN if result else leds.RED)
        if result:
            self.events.ws_serial("Finished!")
            info = self.ctx.ap_info
            self.events.ws_serial(f"AP is now type {info.type:02X} ({ap_type_name(info.type)}), "
                                  f"firmware version {info.version:04X}")
            self.events.ws_serial("[autoreboot]")
            await asyncio.sleep(1.5)
            if self.ctx.request_shutdown is not None:
                self.ctx.request_shutdown()
            return
        if self.ctx.ap_info.version == 0:
            self.events.ws_serial("AP failed to come online. :-(")
        else:
            self.events.ws_serial("Flashing failed. :-(")
        self.events.ws_serial("[reboot]")

    # ------------------------------------------------------------------
    def _download(self, url: str, target: Path) -> bool:
        self.events.ws_serial(f"downloading {target.name}")
        for attempt in range(10):
            try:
                response = requests.get(url, timeout=30, stream=True)
                if response.status_code != 200:
                    self.events.ws_serial(f"http error {response.status_code} fetching {url}")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("wb") as handle:
                        for chunk in response.iter_content(65536):
                            handle.write(chunk)
                    if target.stat().st_size > 0:
                        return True
                    self.events.ws_serial(f"Download failed, {target.stat().st_size} bytes")
            except requests.RequestException as exc:
                self.events.ws_serial(f"download error: {exc}")
            if attempt < 9:
                self.events.ws_serial(f"Retry {attempt}")
        return False

    def _prepare_files(self, url: str) -> list[tuple[Path, int]] | None:
        """Download (or locate) the manifest and binaries; returns (path, address) pairs."""
        staging = self.staging_dir
        if url:
            if not self._download(url.rstrip("/") + "/" + MANIFEST_NAME, staging / MANIFEST_NAME):
                return None
            manifest_path = staging / MANIFEST_NAME
        else:
            manifest_path = staging / MANIFEST_NAME
            if not manifest_path.exists():
                manifest_path = Path(self.settings.c6_binaries_dir) / MANIFEST_NAME
            if not manifest_path.exists():
                self.events.ws_serial(f"load {MANIFEST_NAME}: Failed to open file")
                return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.events.ws_serial(f"json error parsing {MANIFEST_NAME}")
            return None
        if not isinstance(manifest, list):
            self.events.ws_serial(f"json error parsing {MANIFEST_NAME}")
            return None

        files: list[tuple[Path, int]] = []
        for entry in manifest:
            if not isinstance(entry, dict):
                continue
            remote = str(entry.get("filename", ""))
            version = str(entry.get("version", "") or "")
            self.events.ws_serial(f"  {remote}" + (f" (version {version})" if version else ""))
            try:
                address = int(str(entry.get("address", "0")), 16)
            except ValueError:
                self.events.ws_serial(f"invalid address for {remote}")
                return None
            local = manifest_path.parent / Path(remote).name
            if url:
                if not self._download(url.rstrip("/") + "/" + remote, staging / Path(remote).name):
                    return None
                local = staging / Path(remote).name
            if not local.exists():
                self.events.ws_serial(f"file open error {local}")
                return None
            files.append((local, address))
        return files

    async def _flash(self, url: str) -> bool:
        files = await asyncio.to_thread(self._prepare_files, url)
        if not files:
            return False

        serial = self.ctx.serial
        if serial is not None:
            self.events.ws_serial("Entering download mode")
            await asyncio.to_thread(serial.gpio.enter_bootloader)
            await asyncio.sleep(0.3)

        esptool_cmd = self._esptool_command(files)
        if esptool_cmd is None:
            self.events.ws_serial("esptool not available in the virtual environment")
            return False
        self.events.ws_serial("Connecting to the C6 ...")
        ok = await self._run_esptool(esptool_cmd)

        if serial is not None:
            await asyncio.to_thread(serial.gpio.hard_reset)
        return ok

    def _esptool_command(self, files: list[tuple[Path, int]]) -> list[str] | None:
        # ``python -m esptool`` needs the module in this interpreter's environment.
        if importlib.util.find_spec("esptool") is None:
            return None
        cmd = [sys.executable, "-m", "esptool", "--chip", "esp32c6", "--port", self.settings.serial_debug_port,
               "--baud", "115200", "--before", "no_reset", "--after", "no_reset", "write_flash"]
        for path, address in files:
            cmd += [f"0x{address:X}", str(path)]
        return cmd

    async def _run_esptool(self, cmd: list[str]) -> bool:
        log.info("running %s", " ".join(cmd))
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        last_progress = -1
        connected = False
        assert process.stdout is not None
        buffer = b""
        while True:
            chunk = await process.stdout.read(256)
            if not chunk:
                break
            buffer += chunk
            # esptool rewrites the progress line with \r; split on both
            while True:
                index = min((i for i in (buffer.find(b"\n"), buffer.find(b"\r")) if i != -1), default=-1)
                if index == -1:
                    break
                line = buffer[:index].decode("utf-8", errors="replace").strip()
                buffer = buffer[index + 1:]
                if not line:
                    continue
                match = _PROGRESS_RE.search(line)
                if match and line.startswith("Writing"):
                    progress = int(match.group(1))
                    if progress != last_progress and (progress % 5 == 0 or progress == 100):
                        last_progress = progress
                        self.events.ws_serial(f"Progress: {progress}%")
                    continue
                if line.startswith("Chip is"):
                    connected = True
                    self.events.ws_serial("Connected to ESP32-C6")
                self.events.ws_serial(line)
        rc = await process.wait()
        if buffer.strip():
            self.events.ws_serial(buffer.decode("utf-8", errors="replace").strip())
        if rc != 0:
            if not connected:
                self.events.ws_serial("Connection to the C6 failed")
            self.events.ws_serial(f"esptool exited with code {rc}")
            return False
        self.events.ws_serial("Progress: 100%")
        self.events.ws_serial("Flash verified")
        return True
