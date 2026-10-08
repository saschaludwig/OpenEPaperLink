"""The content scheduler (``contentRunner`` / ``drawNew`` / ``checkVars``).

Every second the manager walks the tag database and renders new content for
tags whose ``nextupdate`` has passed.  Rendering and HTTP access are blocking
and run in a worker thread; the resulting file is handed to
:class:`~oepl_linux_ap.newproto.NewProto` on the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from typing import TYPE_CHECKING

from .. import protocol as P
from ..image.encode import (
    EPD_LUT_DEFAULT, EPD_LUT_FAST, EPD_LUT_FAST_NO_REDS, EPD_LUT_NO_REPEATS, SHORTLUT_DISABLED,
    SHORTLUT_ONLY_BLACK, ImageParams, jpg_to_file,
)
from ..image.sprite import get_color
from ..storage import ContentFS
from ..tagdb import TagRecord
from . import calendar as calendar_content
from . import cards, dayahead, rawdata, template
from .drawing import Renderer
from .util import cfg_int, cfg_str, get_midnight_time, is_empty_or_null, is_sleeping, now_epoch

if TYPE_CHECKING:  # pragma: no cover
    from ..state import AppContext

log = logging.getLogger(__name__)

NEVER = 3216153600  # "never update again" sentinel used by the firmware
MIN_FREE_SPACE = 31000


class ContentManager:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.fs = ContentFS(ctx.settings)
        self.renderer = Renderer(ctx)
        self._draw_lock = asyncio.Lock()
        self._ap_mac = self._own_mac()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @property
    def db(self):
        return self.ctx.db

    @property
    def proto(self):
        return self.ctx.proto

    def _own_mac(self) -> bytes:
        """The AP's own 8 byte MAC (Ethernet MAC + two zero bytes)."""
        try:
            from pathlib import Path
            for iface in sorted(Path("/sys/class/net").iterdir()):
                if iface.name == "lo":
                    continue
                text = (iface / "address").read_text().strip()
                raw = bytes(int(part, 16) for part in text.split(":"))
                if len(raw) == 6 and any(raw):
                    return raw + b"\x00\x00"
        except (OSError, ValueError):
            pass
        return b"\x00" * 8

    def _free_space(self) -> int:
        try:
            return shutil.disk_usage(self.ctx.settings.data_path).free
        except OSError:
            return 0

    @staticmethod
    def need_redraw(content_mode: int, wakeup_reason: int) -> bool:
        return content_mode == 26 and wakeup_reason in (
            P.WAKEUP_REASON_BUTTON1, P.WAKEUP_REASON_BUTTON2, P.WAKEUP_REASON_BUTTON3)

    # ------------------------------------------------------------------
    # scheduler
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Main loop: ``contentRunner`` every second, ``checkVars`` every 10 s."""
        last_vars = time.monotonic()
        while True:
            try:
                ap_state = self.ctx.ap_info.state
                if ap_state in (P.AP_STATE_ONLINE, P.AP_STATE_NORADIO):
                    await self.content_runner()
                if time.monotonic() - last_vars >= 10:
                    last_vars = time.monotonic()
                    self.check_vars()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("content runner failed")
            await asyncio.sleep(1)

    async def content_runner(self) -> None:
        cfg = self.db.config
        if cfg.run_status == P.RUNSTATUS_STOP:
            return
        now = now_epoch()
        sleeping = is_sleeping(cfg.sleeptime1, cfg.sleeptime2)
        client_count = self.ctx.hub.client_count

        for record in list(self.db.records):
            if record.version != 0:
                continue
            is_ap = record.mac == self._ap_mac
            if (record.rssi
                    and (now >= record.nextupdate or self.need_redraw(record.content_mode, record.wakeup_reason))
                    and cfg.run_status == P.RUNSTATUS_RUN
                    and (record.expected_next_checkin < now + 300 or is_ap or (client_count and cfg.stopsleep == 1))
                    and self._free_space() > MIN_FREE_SPACE and not sleeping):
                await self.draw_new(record)
                record.wakeup_reason = 0

            if (now - 10 < record.expected_next_checkin < now + 30 and record.pending_idle == 0
                    and record.pending_count == 0 and not is_ap):
                minutes = (record.nextupdate - now) // 60
                if minutes > cfg.maxsleep:
                    minutes = cfg.maxsleep
                if sleeping:
                    local = time.localtime(now)
                    wake = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, cfg.sleeptime2, 0, 0,
                                        0, 0, -1))
                    if wake < now:
                        wake += 24 * 3600
                    minutes = int((wake - now) // 60) - 2
                if minutes > 1 and (client_count == 0 or cfg.stopsleep == 0):
                    record.pending_idle = minutes * 60
                    record.expected_next_checkin = now + record.pending_idle
                    if not record.is_external and self.proto is not None:
                        await self.proto.prepare_idle_req(record.mac, minutes)
            await asyncio.sleep(0)

    def check_vars(self) -> None:
        """``checkVars``: schedule redraws when referenced variables changed."""
        changed = [name for name, entry in self.db.vars.items() if entry.changed]
        if changed:
            for record in self.db.records:
                if record.content_mode == 19:
                    try:
                        cfg = json.loads(record.mode_config_json or "{}")
                    except json.JSONDecodeError:
                        cfg = {}
                    jsonfile = cfg_str(cfg, "filename")
                    if not is_empty_or_null(jsonfile):
                        content = self.fs.read_bytes(ContentFS.normalise(jsonfile))
                        if content is not None:
                            text = content.decode("utf-8", errors="replace")
                            for name in changed:
                                if name in text:
                                    log.info("updating %s because of var %s", jsonfile, name)
                                    record.nextupdate = 0
                                    break
                if record.content_mode == 21 and any(n in changed for n in ("ap_tagcount", "ap_ip", "ap_ch")):
                    record.nextupdate = 0
        for _name, entry in self.db.vars.items():
            entry.changed = False

    # ------------------------------------------------------------------
    # drawNew
    # ------------------------------------------------------------------
    def _image_params(self, record: TagRecord, hw, now: int) -> ImageParams:
        params = ImageParams.for_hwtype(hw)
        params.highlight_color = get_color(str(hw.highlight_color))
        params.has_red = False
        params.data_type = P.DATATYPE_IMG_RAW_1BPP
        params.dither = 2
        params.invert = record.invert
        params.symbols = 0
        params.rotate = record.rotate
        params.zlib = 1 if hw.zlib != 0 and record.tag_software_version >= hw.zlib else 0
        params.g5 = 1 if hw.g5 != 0 and record.tag_software_version >= hw.g5 else 0

        params.lut = EPD_LUT_NO_REPEATS
        if record.lut == 2:
            params.lut = EPD_LUT_FAST_NO_REDS
        if record.lut == 3:
            params.lut = EPD_LUT_FAST
        last_midnight = now - now % (24 * 60 * 60) + 3 * 3600
        if params.shortlut == SHORTLUT_DISABLED or record.last_full_update < last_midnight or record.lut == 1:
            params.lut = EPD_LUT_DEFAULT
            record.last_full_update = now
        return params

    def _apply_timestamp_option(self, params: ImageParams, record: TagRecord) -> None:
        params.ts_option = self.db.config.showtimestamp
        if not params.ts_option:
            return
        loc = self.renderer.get_template(record.content_mode, record.hw_type)
        if isinstance(loc, dict) and isinstance(loc.get("ts_option"), int):
            params.ts_option = loc["ts_option"]
        elif isinstance(loc, list):
            for elem in loc:
                if isinstance(elem, dict) and isinstance(elem.get("ts_option"), int):
                    params.ts_option = elem["ts_option"]
                    break

    @staticmethod
    def _interval_seconds(cfg: dict, now: int) -> int:
        interval = cfg_int(cfg, "interval") * 60
        if interval == -1440 * 60:
            interval = get_midnight_time() - now
        elif interval < 0:
            interval = -interval
            interval = (interval - (now % interval)) % interval
        elif interval < 180:
            interval = 60 * 60
        return interval

    def _select_data_type(self, params: ImageParams) -> None:
        if params.has_red and params.lut == EPD_LUT_NO_REPEATS and params.shortlut == SHORTLUT_ONLY_BLACK:
            params.lut = EPD_LUT_DEFAULT
        if params.bpp == 3:
            params.data_type = P.DATATYPE_IMG_RAW_3BPP
        elif params.bpp == 4:
            params.data_type = P.DATATYPE_IMG_RAW_4BPP
        elif params.zlib:
            params.data_type = P.DATATYPE_IMG_ZLIB
        elif params.g5:
            params.data_type = P.DATATYPE_IMG_G5
        elif params.has_red:
            params.data_type = P.DATATYPE_IMG_RAW_2BPP

    async def update_tag_image(self, filename: str, record: TagRecord, next_checkin: int,
                               params: ImageParams) -> bool:
        """``updateTagImage``: hand the rendered file (or segments) to the protocol layer."""
        if self.proto is None:
            return False
        if record.hw_type == P.SOLUM_SEG_UK:
            await self.proto.send_ap_segmented_data(record.mac, params.segments, params.symbols,
                                                    params.invert == 1, not record.is_external)
            return True
        self._select_data_type(params)
        if next_checkin > 0x7FFF or next_checkin < 0:
            next_checkin = 0
        await self.proto.prepare_data_avail_file(filename, params.data_type, params.lut, record.mac, next_checkin)
        return True

    async def draw_new(self, record: TagRecord) -> None:
        async with self._draw_lock:
            try:
                await self._draw_new(record)
            except Exception:  # noqa: BLE001
                log.exception("drawNew failed for %s", P.mac_to_hex(record.mac))
                self.ctx.events.ws_err(f"content error for {P.mac_to_hex(record.mac)}")
                record.nextupdate = now_epoch() + 300

    async def _draw_new(self, record: TagRecord) -> None:
        now = now_epoch()
        hw = self.db.tagtypes.get(record.hw_type)
        if hw.bpp == 0:
            record.nextupdate = now + 300
            log.info("No definition found for tag type %d", record.hw_type)
            return

        is_ap = record.mac == self._ap_mac
        if (record.wakeup_reason in (P.WAKEUP_REASON_FIRSTBOOT, P.WAKEUP_REASON_WDT_RESET)
                and record.content_mode == 0):
            if is_ap:
                record.content_mode = 21
                record.nextupdate = 0
            elif self.fs.exists("/tag_defaults.json"):
                try:
                    defaults = json.loads(self.fs.read_bytes("/tag_defaults.json") or b"{}")
                    if isinstance(defaults.get("contentMode"), int):
                        record.content_mode = defaults["contentMode"]
                    if isinstance(defaults.get("modecfgjson"), str):
                        record.mode_config_json = defaults["modecfgjson"]
                except json.JSONDecodeError:
                    pass

        hexmac = P.mac_to_hex(record.mac)
        filename = f"/temp/{hexmac}.raw"
        self.fs.path("/temp").mkdir(parents=True, exist_ok=True)
        try:
            cfg = json.loads(record.mode_config_json) if record.mode_config_json else {}
        except json.JSONDecodeError:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}

        self.ctx.events.ws_log("Updating " + hexmac)
        record.nextupdate = now + 60

        params = self._image_params(record, hw, now)
        interval = self._interval_seconds(cfg, now)
        self._apply_timestamp_option(params, record)
        local = not record.is_external
        mode = record.content_mode

        if mode in (0, 22, 23, 24, 25):
            await self._draw_static_image(record, cfg, filename, params)
        elif mode == 1:
            await asyncio.to_thread(cards.draw_date, self, filename, cfg, record, params)
            record.nextupdate = get_midnight_time()
            await self.update_tag_image(filename, record, (record.nextupdate - now) // 60 - 10, params)
        elif mode == 2:
            await self._draw_counter(record, cfg, filename, params, get_midnight_time(), 15)
        elif mode == 3:
            await self._draw_counter(record, cfg, filename, params, now + 3600, 5)
        elif mode == 4:
            await asyncio.to_thread(cards.draw_weather, self, filename, cfg, record, params)
            record.nextupdate = now + interval
            await self.update_tag_image(filename, record, interval // 60, params)
        elif mode == 8:
            await asyncio.to_thread(cards.draw_forecast, self, filename, cfg, record, params)
            record.nextupdate = now + interval
            await self.update_tag_image(filename, record, interval // 60, params)
        elif mode == 5:
            await self._send_firmware(record, cfg)
        elif mode == 7:
            code = await asyncio.to_thread(cards.get_img_url, self, filename, cfg_str(cfg, "url"),
                                           cfg_int(cfg, "#fetched"), params, hexmac)
            if code == 200:
                record.nextupdate = now + interval
                await self.update_tag_image(filename, record, interval // 60, params)
                cfg["#fetched"] = now
            elif code == 304:
                record.nextupdate = now + interval
            else:
                record.nextupdate = now + 300
        elif mode == 9:
            ok = await asyncio.to_thread(cards.get_rss_feed, self, filename, cfg_str(cfg, "url"),
                                         cfg_str(cfg, "title"), record, params)
            if ok:
                record.nextupdate = now + interval
                await self.update_tag_image(filename, record, interval // 60, params)
            else:
                record.nextupdate = now + 300
        elif mode == 10:
            await asyncio.to_thread(cards.draw_qr, self, filename, cfg_str(cfg, "qr-content"), cfg_str(cfg, "title"),
                                    record, params)
            record.nextupdate = now + 12 * 3600
            await self.update_tag_image(filename, record, 0, params)
        elif mode == 11:
            ok = await asyncio.to_thread(calendar_content.get_cal_feed, self, filename, cfg, record, params)
            if ok:
                record.nextupdate = now + interval
                await self.update_tag_image(filename, record, interval // 60, params)
            else:
                record.nextupdate = now + 300
        elif mode == 12:
            record.nextupdate = NEVER
        elif mode == 13:
            text = f"{cfg_str(cfg, 'line1'):<4.4}{cfg_str(cfg, 'line2'):<2.2}{cfg_str(cfg, 'line3'):<4.4}"
            record.nextupdate = NEVER
            if self.proto is not None:
                await self.proto.send_ap_segmented_data(record.mac, text, 0x0000, False, local)
        elif mode == 14:
            record.nextupdate = NEVER
            if self.proto is not None:
                await self.proto.prepare_data_avail_bytes(rawdata.nfc_url_payload(cfg_str(cfg, "url")),
                                                          P.DATATYPE_NFC_RAW_CONTENT, record.mac)
        elif mode == 16:
            refresh = await asyncio.to_thread(cards.draw_buienradar, self, filename, cfg, record, params)
            record.nextupdate = now + refresh * 60
            await self.update_tag_image(filename, record, refresh, params)
        elif mode == 17:
            if self.proto is not None:
                await self.proto.send_tag_command(record.mac, cfg_int(cfg, "cmd"), local)
            record.nextupdate = NEVER
        elif mode == 18:
            if self.proto is not None:
                await self.proto.prepare_data_avail_bytes(rawdata.tag_config_payload(cfg),
                                                          P.DATATYPE_TAG_CONFIG_DATA, record.mac)
            record.nextupdate = NEVER
        elif mode == 19:
            await self._draw_json_template(record, cfg, filename, params, interval, hexmac, now)
        elif mode == 20:
            pass  # display a copy: handled by check_mirror when the source updates
        elif mode == 21:
            await asyncio.to_thread(cards.draw_ap_info, self, filename, cfg, record, params)
            await self.update_tag_image(filename, record, 0, params)
            record.nextupdate = NEVER
        elif mode == 26:
            record.nextupdate = NEVER
            preload: list[tuple[str, int]] = []
            await asyncio.to_thread(cards.draw_timestamp, self, filename, cfg, record, params, preload)
            for preload_file, special_type in preload:
                preload_params = ImageParams(**{**params.__dict__})
                self._select_data_type(preload_params)
                arg = P.image_data_type_arg(0, 1, special_type)
                if self.proto is not None:
                    await self.proto.prepare_data_avail_file(preload_file, preload_params.data_type, arg,
                                                             record.mac, 5 | 0x8000)
            await self.update_tag_image(filename, record, 0, params)
        elif mode == 27:
            ok = await asyncio.to_thread(dayahead.get_day_ahead_feed, self, filename, cfg, record, params)
            if ok:
                minutes = 15
                freq = cfg_str(cfg, "updatefreq").strip()
                if freq:
                    minutes = cfg_int(cfg, "updatefreq", 15)
                    if minutes < 1:
                        self.ctx.events.ws_err("Invalid update frequency, defaulting to 15 minutes")
                        minutes = 15
                update_interval = minutes * 60
                record.nextupdate = now + (update_interval - now % update_interval)
                await self.update_tag_image(filename, record, 0, params)
            else:
                record.nextupdate = now + 300
        elif mode == 28:
            try:
                new_mac = int(cfg_str(cfg, "mac").strip() or "0", 16)
            except ValueError:
                new_mac = 0
            if self.proto is not None:
                await self.proto.send_tag_mac(record.mac, new_mac, local)
            record.nextupdate = NEVER
        elif mode == 29:
            record.nextupdate = now + 1800
            if self.proto is not None:
                await self.proto.prepare_data_avail_bytes(rawdata.time_raw_payload(now),
                                                          P.DATATYPE_CUSTOM_LUT_OTA, record.mac)

        record.mode_config_json = json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)

    # ------------------------------------------------------------------
    # individual modes that need protocol access
    # ------------------------------------------------------------------
    async def _draw_static_image(self, record: TagRecord, cfg: dict, filename: str, params: ImageParams) -> None:
        config_filename = cfg_str(cfg, "filename")
        if is_empty_or_null(config_filename):
            log.info("Resend static image: nothing to do")
            record.nextupdate = NEVER
            return
        config_filename = ContentFS.normalise(config_filename)
        if not self.fs.exists(config_filename):
            record.nextupdate = NEVER
            self.ctx.events.ws_err("Not found: " + config_filename)
            return

        params.dither = cfg_int(cfg, "dither")
        params.preload = cfg_str(cfg, "preload") == "1"
        params.preloadlut = cfg_int(cfg, "preload_lut")
        params.preloadtype = cfg_int(cfg, "preload_type")

        ok = await asyncio.to_thread(jpg_to_file, self.fs.path(config_filename), self.fs.path(filename), params,
                                     self.renderer.fonts)
        if not ok:
            self.ctx.events.ws_err("invalid jpg")
            record.nextupdate = NEVER
            return

        if params.has_red and params.lut == EPD_LUT_NO_REPEATS and params.shortlut == SHORTLUT_ONLY_BLACK:
            params.lut = EPD_LUT_DEFAULT
        if params.bpp == 3:
            params.data_type = P.DATATYPE_IMG_RAW_3BPP
        elif params.bpp == 4:
            params.data_type = P.DATATYPE_IMG_RAW_4BPP
        elif params.zlib:
            params.data_type = P.DATATYPE_IMG_ZLIB
        elif params.g5:
            params.data_type = P.DATATYPE_IMG_G5
        elif params.has_red:
            params.data_type = P.DATATYPE_IMG_RAW_2BPP
        log.info("datatype: 0x%02X", params.data_type)

        if params.preload:
            arg = P.image_data_type_arg(params.preloadlut, 1, params.preloadtype)
        else:
            arg = P.image_data_type_arg(params.lut & 0x03, 0, 0)

        if self.proto is not None:
            ok = await self.proto.prepare_data_avail_file(filename, params.data_type, arg, record.mac,
                                                          cfg_int(cfg, "timetolive"))
            if ok:
                if cfg_str(cfg, "delete") == "1":
                    self.fs.remove(config_filename)
            else:
                self.ctx.events.ws_err("Error accessing " + filename)
        record.nextupdate = NEVER

    async def _draw_counter(self, record: TagRecord, cfg: dict, filename: str, params: ImageParams,
                            next_update: int, next_checkin: int) -> None:
        counter = cfg_int(cfg, "counter")
        await asyncio.to_thread(cards.draw_number, self, filename, counter, cfg_int(cfg, "thresholdred"), record,
                                params)
        record.nextupdate = next_update
        await self.update_tag_image(filename, record, next_checkin, params)
        cfg["counter"] = counter + 1

    async def _send_firmware(self, record: TagRecord, cfg: dict) -> None:
        now = now_epoch()
        filename = cfg_str(cfg, "filename")
        if not is_empty_or_null(filename) and not cfg.get("#fetched"):
            content = self.fs.read_bytes(ContentFS.normalise(filename))
            if content is not None:
                if b"<html" in content:
                    self.ctx.events.ws_err("User error flashing tag firmware: this is a html-file!")
                    cfg["#fetched"] = True
                elif self.proto is not None:
                    ok = await self.proto.prepare_data_avail_file(filename, P.DATATYPE_FW_UPDATE, 0, record.mac,
                                                                  cfg_int(cfg, "timetolive"))
                    if ok:
                        cfg["#fetched"] = True
            record.nextupdate = NEVER
        else:
            record.nextupdate = now + 300

    async def _draw_json_template(self, record: TagRecord, cfg: dict, filename: str, params: ImageParams,
                                  interval: int, hexmac: str, now: int) -> None:
        from .util import http_get_json

        config_filename = cfg_str(cfg, "filename")
        if not is_empty_or_null(config_filename):
            config_url = cfg_str(cfg, "url")
            if not is_empty_or_null(config_url):
                config_url = config_url.replace("{mac}", hexmac)
                variables = await asyncio.to_thread(http_get_json, config_url, 1.0)
                if variables is not None:
                    record.nextupdate = now + interval
                    ok = await asyncio.to_thread(template.get_json_template_file, self, filename, config_filename,
                                                 record, params, variables)
                    if ok:
                        await self.update_tag_image(filename, record, interval // 60, params)
                    else:
                        self.ctx.events.ws_err("error opening file " + config_filename)
                else:
                    record.nextupdate = now + 600
            else:
                ok = await asyncio.to_thread(template.get_json_template_file, self, filename, config_filename,
                                             record, params)
                if ok:
                    await self.update_tag_image(filename, record, interval, params)
                else:
                    self.ctx.events.ws_err("error opening file " + config_filename)
                record.nextupdate = NEVER
        else:
            code = await asyncio.to_thread(template.get_json_template_url, self, filename, cfg_str(cfg, "url"),
                                           cfg_int(cfg, "#fetched"), hexmac, record, params)
            if code == 200:
                record.nextupdate = now + interval
                await self.update_tag_image(filename, record, interval // 60, params)
                cfg["#fetched"] = now
            elif code == 304:
                record.nextupdate = now + interval
            else:
                record.nextupdate = now + 600
