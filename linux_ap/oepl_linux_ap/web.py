"""HTTP and WebSocket server (``web.cpp``, ``ota.cpp``, ``SPIFFSEditor.cpp``).

Every endpoint keeps the request parameters and response shapes of the ESP
firmware so the unmodified ``wwwroot`` front end works against it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import __version__
from . import leds
from .protocol import (
    AP_STATE_NORADIO,
    AP_STATE_ONLINE,
    CMD_DO_DEEPSLEEP,
    CMD_DO_LEDFLASH,
    CMD_DO_REBOOT,
    CMD_DO_RESET_SETTINGS,
    CMD_DO_SCAN,
    NO_UPDATE_SENTINEL,
    RUNSTATUS_RUN,
    RUNSTATUS_STOP,
    SYNC_DELETE,
    SYNC_TAGSTATUS,
    SYNC_USERCFG,
    hex_to_mac,
    pack_led_flash,
)
from .state import AppContext, ap_type_name

log = logging.getLogger(__name__)

CTX_KEY = web.AppKey("ctx", AppContext)

CONTENT_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".raw": "application/octet-stream",
    ".bin": "application/octet-stream",
    ".pending": "application/octet-stream",
    ".vlw": "application/octet-stream",
    ".ttf": "font/ttf",
}


def _ctx(request: web.Request) -> AppContext:
    return request.app[CTX_KEY]


def _text(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, status=status, content_type="text/plain")


def _json(payload: Any, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(payload, ensure_ascii=False), status=status,
                        content_type="application/json")


def _mac_or_none(value: str | None) -> bytes | None:
    if not value:
        return None
    try:
        return hex_to_mac(value)
    except ValueError:
        return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


def _resolve_fs_path(ctx: AppContext, path: str) -> Path | None:
    """Map a LittleFS style absolute path onto the data directory.

    ``/www/...`` and ``/fonts/...`` fall back to the repository assets when
    the data directory does not contain an override.
    """
    rel = path.lstrip("/")
    if ".." in Path(rel).parts:
        return None
    candidate = ctx.settings.data_path / rel
    if candidate.exists():
        return candidate
    if rel.startswith("www/"):
        alt = Path(ctx.settings.wwwroot_dir) / rel[4:]
        if alt.exists():
            return alt
    if rel.startswith("fonts/"):
        for base in ctx.settings.font_search_paths():
            alt = base / rel[6:]
            if alt.exists():
                return alt
    if rel.startswith("tagtypes/"):
        alt = Path(ctx.settings.tagtypes_seed_dir) / rel[9:]
        if alt.exists():
            return alt
    return candidate


def _write_path(ctx: AppContext, path: str) -> Path | None:
    rel = path.lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    return ctx.settings.data_path / rel


def _file_response(path: Path, download: bool = False, cache: str | None = None) -> web.StreamResponse:
    headers = {}
    if cache:
        headers["Cache-Control"] = cache
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return web.FileResponse(path, headers=headers | {"Content-Type": content_type})


# --------------------------------------------------------------------------
# Static content
# --------------------------------------------------------------------------
async def handle_index(request: web.Request) -> web.StreamResponse:
    ctx = _ctx(request)
    index = ctx.settings.www_path / "index.html"
    if not index.exists():
        return web.Response(text="index.html not found. Did you forget to upload the littlefs partition?",
                            content_type="text/html")
    return _file_response(index)


async def handle_www(request: web.Request) -> web.StreamResponse:
    ctx = _ctx(request)
    name = request.match_info["name"]
    if ".." in Path(name).parts:
        raise web.HTTPNotFound()
    path = ctx.settings.www_path / name
    if path.is_dir():
        path = path / "index.html"
    if not path.is_file():
        override = _resolve_fs_path(ctx, "/www/" + name)
        if override is not None and override.is_file():
            path = override
        else:
            raise web.HTTPNotFound()
    return _file_response(path)


async def handle_current(request: web.Request) -> web.StreamResponse:
    ctx = _ctx(request)
    name = request.match_info["name"]
    if ".." in Path(name).parts:
        raise web.HTTPNotFound()
    path = ctx.settings.current_dir / name
    if not path.is_file():
        raise web.HTTPNotFound()
    return _file_response(path, cache="max-age=604800")


async def handle_tagtypes(request: web.Request) -> web.StreamResponse:
    ctx = _ctx(request)
    name = request.match_info["name"]
    path = _resolve_fs_path(ctx, "/tagtypes/" + name)
    if path is None or not path.is_file():
        raise web.HTTPNotFound()
    return _file_response(path, cache="max-age=300")


async def handle_setup(request: web.Request) -> web.StreamResponse:
    ctx = _ctx(request)
    path = ctx.settings.www_path / "setup.html"
    if not path.is_file():
        raise web.HTTPNotFound()
    return _file_response(path)


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------
async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ctx = _ctx(request)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    ctx.hub.add_client(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
            # The ESP only consumes WS data for the external flasher, which
            # the NanoPi build does not have.
    finally:
        ctx.hub.remove_client(ws)
    return ws


def build_sysinfo_payload(ctx: AppContext, force_counts: bool = False) -> dict[str, Any]:
    """``wsSendSysteminfo``: periodic status message for the dashboard."""
    now = int(time.time())
    db = ctx.db
    cfg = db.config
    payload: dict[str, Any] = {
        "currtime": now,
        "heap": _free_memory(),
        "recordcount": len(db.records),
        "dbsize": _db_size(ctx),
        "littlefsfree": _free_disk(ctx.settings.data_path),
        "apstate": ctx.ap_info.state,
        "runstate": cfg.run_status,
        "rssi": 0,
        "wifistatus": 3,  # WL_CONNECTED – the UI treats Ethernet the same way
        "wifissid": "Ethernet",
        "uptime": ctx.uptime,
    }

    local = time.localtime(now)
    db.vars.set("ap_date", time.strftime(ctx.language.date_format_long, local))
    db.vars.set("ap_ip", _local_ip())
    channel_text = str(ctx.ap_info.channel)
    if ctx.ap_info.has_subghz:
        channel_text += ", SubGhz " + ("disabled" if ctx.ap_info.subghz_channel == 0
                                       else str(ctx.ap_info.subghz_channel))
    db.vars.set("ap_ch", channel_text)

    state = ctx.__dict__.setdefault("_sysinfo_state", {"tagcount_at": 0.0})
    if force_counts or time.monotonic() - state["tagcount_at"] > 60 or state["tagcount_at"] == 0:
        tagcount, timeoutcount, lowbatt = db.tag_count()
        payload["lowbattcount"] = lowbatt
        payload["timeoutcount"] = timeoutcount
        if timeoutcount > 0:
            db.vars.set("ap_tagcount", f"{tagcount}/{len(db.records)}, {timeoutcount} timeout")
        else:
            db.vars.set("ap_tagcount", f"{tagcount} / {len(db.records)}")
        # The firmware tints the idle LED dark blue while tags are timing out.
        if ctx.leds is not None and ctx.ap_info.state == AP_STATE_ONLINE and ctx.ap_info.is_online:
            ctx.leds.set_idle(leds.DARK_BLUE if timeoutcount > 0 else leds.GREEN)
        state["tagcount_at"] = time.monotonic()
    return payload


def _free_memory() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _free_disk(path: Path) -> int:
    try:
        stat = os.statvfs(path if path.exists() else path.parent)
        return stat.f_bavail * stat.f_frsize
    except OSError:
        return 0


def _db_size(ctx: AppContext) -> int:
    size = 0
    for record in ctx.db.records:
        size += 180 + len(record.mode_config_json) + len(record.alias)
        if record.data:
            size += len(record.data)
    return size


def _local_ip() -> str:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return "0.0.0.0"


# --------------------------------------------------------------------------
# Tag database endpoints
# --------------------------------------------------------------------------
async def handle_get_db(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    if "mac" in request.query:
        mac = _mac_or_none(request.query.get("mac"))
        if mac is None:
            return _json({"error": "malformatted parameter"})
        return _json(ctx.db.to_json(mac))
    pos = _int(request.query.get("pos", 0))
    return _json(ctx.db.to_json(None, pos))


async def handle_getdata(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    mac = _mac_or_none(request.query.get("mac"))
    if mac is not None:
        record = ctx.db.find(mac)
        if record is not None and ctx.proto is not None:
            if "md5" in request.query:
                md5_bytes = _mac_or_none(request.query.get("md5"))
                if md5_bytes is not None:
                    data_ver = int.from_bytes(md5_bytes, "little")
                    item = ctx.proto.get_queue_item(mac, data_ver)
                    if item is None:
                        return _text("File not found", 404)
                    data = ctx.proto.load_item_data(item)
                    if data is None:
                        return _text("File not found", 404)
                    return web.Response(body=data, content_type="application/octet-stream")
            else:
                data = ctx.proto.load_record_data(record)
                if data is None:
                    return _text("File not found", 404)
                return web.Response(body=data, content_type="application/octet-stream")
    return _text("No data available", 400)


async def handle_save_cfg(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    form = await request.post()
    mac = _mac_or_none(form.get("mac"))
    if mac is not None:
        record = ctx.db.find(mac)
        if record is None:
            return _text("Error while saving: mac not found")
        if "contentmode" in form:
            new_mode = _int(form["contentmode"])
            if new_mode != record.content_mode and new_mode in (5, 17, 18):
                ctx.db.push_taginfo(record)
            record.content_mode = new_mode
        if "alias" in form:
            record.alias = str(form["alias"])
        if "modecfgjson" in form:
            record.mode_config_json = str(form["modecfgjson"])
        record.nextupdate = 0
        if "rotate" in form:
            record.rotate = _int(form["rotate"])
        if "lut" in form:
            record.lut = _int(form["lut"])
        if "invert" in form:
            record.invert = _int(form["invert"])
        ctx.ws_send_taginfo(mac, SYNC_USERCFG)
    return _text("Ok, saved")


async def handle_tag_cmd(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    form = await request.post()
    if "mac" not in form or "cmd" not in form:
        return _text("param error", 500)
    mac = _mac_or_none(form.get("mac"))
    if mac is None:
        return _text("Error: mac not found", 400)
    record = ctx.db.find(mac)
    if record is None:
        return _text("Error: mac not found", 400)
    cmd = str(form["cmd"])
    proto = ctx.proto
    local = not record.is_external

    if cmd == "del":
        ctx.ws_send_taginfo(mac, SYNC_DELETE)
        ctx.db.delete(mac)
    elif cmd == "purge":
        now = int(time.time())
        for tag in list(reversed(ctx.db.records)):
            if (tag.expected_next_checkin == NO_UPDATE_SENTINEL or tag.lastseen < now - 24 * 3600
                    or now > tag.expected_next_checkin + 600):
                ctx.ws_send_taginfo(tag.mac, SYNC_DELETE)
                ctx.db.delete(tag.mac)
    elif cmd == "clear":
        ctx.db.clear_pending(record)
        if proto is not None:
            while proto.dequeue_item(mac):
                pass
            record.pending_count = proto.count_queue_item(mac)
        ctx.ws_send_taginfo(mac, SYNC_TAGSTATUS)
    elif cmd == "refresh":
        if proto is not None:
            await proto.update_content(mac)
    elif cmd == "reboot" and proto is not None:
        await proto.send_tag_command(mac, CMD_DO_REBOOT, local)
    elif cmd == "scan" and proto is not None:
        await proto.send_tag_command(mac, CMD_DO_SCAN, local)
    elif cmd == "reset" and proto is not None:
        await proto.send_tag_command(mac, CMD_DO_RESET_SETTINGS, local)
    elif cmd == "deepsleep" and proto is not None:
        await proto.send_tag_command(mac, CMD_DO_DEEPSLEEP, local)
        record.pending_idle = 9999
        ctx.ws_send_taginfo(mac, SYNC_TAGSTATUS)
    elif cmd == "ledflash" and proto is not None:
        # green / red / blue, 3 flashes each, delay 10, repeats 2
        payload = pack_led_flash(1, 8, (0x3C, 0xE4, 0x03), (3, 3, 3), (1, 5, 10), (10, 10, 10), 2)
        await proto.send_tag_command(mac, CMD_DO_LEDFLASH, local, payload)
    elif cmd == "ledflash_long" and proto is not None:
        payload = pack_led_flash(1, 1, (0xE4, 0, 0), (3, 0, 0), (3, 0, 0), (50, 0, 0), 60)
        await proto.send_tag_command(mac, CMD_DO_LEDFLASH, local, payload)
    elif cmd == "ledflash_stop" and proto is not None:
        await proto.send_tag_command(mac, CMD_DO_LEDFLASH, local, bytes(12))
    return _text("Ok, done")


async def handle_led_flash(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    mac = _mac_or_none(request.query.get("mac"))
    if mac is not None:
        record = ctx.db.find(mac)
        if record is not None:
            payload = bytes(12)
            pattern = request.query.get("pattern")
            if pattern is not None:
                try:
                    payload = bytes.fromhex(pattern)
                except ValueError:
                    payload = b""
                if len(payload) != 12:
                    return _text("Error: expects 12 hex bytes in pattern", 400)
            if ctx.proto is not None:
                await ctx.proto.send_tag_command(mac, CMD_DO_LEDFLASH, not record.is_external, payload)
            return _text("ok, request transmitted")
    return _text("parameters are missing", 400)


# --------------------------------------------------------------------------
# AP configuration
# --------------------------------------------------------------------------
async def handle_get_ap_config(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    if ctx.udp is not None:
        ctx.udp.get_ap_list()
    payload: dict[str, Any] = {
        "C6": "1",
        "H2": "0",
        "TLSR": "0",
        "savespace": "0",
        "hasFlasher": "0",
        "hasBLE": "0",
        "hasSubGhz": "1" if ctx.ap_info.has_subghz else "0",
        "apstate": str(ctx.ap_info.state),
    }
    payload.update(ctx.db.config.to_json())
    return _json(payload)


async def handle_save_apcfg(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    form = await request.post()
    cfg = ctx.db.config
    if "alias" in form:
        cfg.alias = str(form["alias"])[:31]
    for name in ("channel", "subghzchannel", "led", "tft", "maxsleep", "stopsleep", "preview", "nightlyreboot",
                 "lock", "ble", "discovery", "showtimestamp", "wifipower"):
        if name in form:
            setattr(cfg, name, _int(form[name]) & 0xFF)
    if "language" in form:
        cfg.language = _int(form["language"]) & 0xFF
        ctx.language.reload(cfg.language)
    if "sleeptime1" in form:
        cfg.sleeptime1 = _int(form["sleeptime1"]) & 0xFF
        cfg.sleeptime2 = _int(form.get("sleeptime2", 0)) & 0xFF
    if "timezone" in form:
        cfg.timezone = str(form["timezone"])[:51]
        os.environ["TZ"] = cfg.timezone
        time.tzset()
    if "repo" in form:
        cfg.repo = str(form["repo"])
    if "env" in form:
        cfg.env = str(form["env"])
    ctx.db.save_config()
    if ctx.leds is not None:
        ctx.leds.set_brightness(cfg.led)
    if ctx.proto is not None:
        await ctx.proto.set_ap_channel()
    return _text("Ok, saved")


async def handle_set_time(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    form = await request.post()
    if "epoch" not in form:
        return _text("missing 'epoch'", 400)
    epoch = _int(form["epoch"])
    if epoch <= 1600000000:
        return _text("invalid epoch", 400)
    try:
        import subprocess

        subprocess.run(["date", "-s", f"@{epoch}"], check=False, capture_output=True)
    except OSError as exc:
        log.warning("set_time failed: %s", exc)
    ctx.events.log_line("Time set via /set_time")
    ctx.events.ws_sysinfo(build_sysinfo_payload(ctx))
    return _text("ok")


async def handle_set_var(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    form = await request.post()
    if "key" in form and "val" in form:
        ctx.db.vars.set(str(form["key"]), str(form["val"]))
        return _text("Ok, saved")
    return _text("param error", 500)


async def handle_set_vars(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    form = await request.post()
    if "json" not in form:
        return _text("No 'json' parameter found in request", 400)
    try:
        doc = json.loads(str(form["json"]))
    except json.JSONDecodeError:
        return _text("Failed to parse JSON", 400)
    if isinstance(doc, dict):
        for key, value in doc.items():
            ctx.db.vars.set(str(key), value if isinstance(value, str) else json.dumps(value))
    return _text("JSON uploaded and processed")


# --------------------------------------------------------------------------
# Ethernet stubs for the WLAN setup page
# --------------------------------------------------------------------------
async def handle_get_wifi_config(request: web.Request) -> web.Response:
    return _json({"ssid": "Ethernet", "pw": "", "ip": _local_ip(), "mask": "", "gw": "", "dns": "",
                  "mac": _ethernet_mac()})


def _ethernet_mac() -> str:
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo":
                continue
            with open(f"/sys/class/net/{name}/address", "r", encoding="ascii") as handle:
                return handle.read().strip().upper()
    except OSError:
        pass
    return "00:00:00:00:00:00"


async def handle_get_ssid_list(request: web.Request) -> web.Response:
    # scanstatus -2 = no scan running / no results; the UI shows an empty list.
    return _json({"scanstatus": -2, "networks": []})


async def handle_save_wifi_config(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    try:
        doc = await request.json()
    except json.JSONDecodeError:
        doc = {}
    if isinstance(doc, dict) and doc.get("ssid") == "factory":
        ctx.events.log_line("Factory reset requested via setup page")
        ctx.db.config.run_status = RUNSTATUS_STOP
        ctx.db.records.clear()
        ctx.db.cleanup_current()
        for name in ("log.txt", "logold.txt", "update_actions.json", "current/tagDB.json",
                     "current/tagDB.json.bak", "current/tagDBrestored.json", "current/apconfig.json"):
            try:
                (ctx.settings.data_path / name).unlink()
            except OSError:
                pass
        asyncio.get_running_loop().call_later(1.0, _request_restart, ctx)
        return _text("Ok, saved")
    ctx.events.ws_log("Network is configured by the operating system on this access point")
    return _text("Ok, saved")


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------
async def _read_multipart(request: web.Request) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Return (fields, files) of a multipart form.  Files map field -> (filename, data)."""
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.filename:
            files[part.name or "file"] = (part.filename, await part.read(decode=False))
        else:
            fields[part.name or ""] = (await part.read(decode=False)).decode("utf-8", "replace")
    return fields, files


async def handle_imgupload(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    if ctx.db.config.run_status != RUNSTATUS_RUN:
        return _text("Come back later", 409)
    fields, files = await _read_multipart(request)
    mac_hex = fields.get("mac")
    mac = _mac_or_none(mac_hex)
    if mac is None:
        return _text("parameters incomplete", 400)
    record = ctx.db.find(mac)
    if record is None:
        return _text("mac not found", 400)
    if not files:
        return _text("parameters incomplete", 400)
    _name, data = next(iter(files.values()))
    upload_name = f"{mac_hex}_{ctx.millis}.jpg"
    temp_dir = ctx.settings.data_path / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    (temp_dir / upload_name).write_bytes(data)
    ctx.events.log_line("http imageUpload " + upload_name)

    dither = _int(fields.get("dither", 1), 1)
    if "alias" in fields:
        record.alias = fields["alias"]
    if "rotate" in fields:
        record.rotate = _int(fields["rotate"])
    if "lut" in fields:
        record.lut = _int(fields["lut"])
    if "invert" in fields:
        record.invert = _int(fields["invert"])
    ttl = _int(fields.get("ttl", 0))
    preload = 0
    preload_lut = 0
    preload_type = 0
    if "preloadtype" in fields:
        preload = 1
        preload_type = _int(fields["preloadtype"])
        preload_lut = _int(fields.get("preloadlut", 0))
    record.mode_config_json = json.dumps({
        "filename": f"/temp/{upload_name}",
        "timetolive": str(ttl),
        "dither": str(dither),
        "delete": "1",
        "preload": str(preload),
        "preload_lut": str(preload_lut),
        "preload_type": str(preload_type),
    })
    record.content_mode = _int(fields["contentmode"]) if "contentmode" in fields else 24
    record.nextupdate = 0
    ctx.ws_send_taginfo(mac, SYNC_USERCFG)
    return _text("Ok, saved")


async def handle_jsonupload(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    if ctx.db.config.run_status != RUNSTATUS_RUN:
        return _text("come back later", 409)
    form = await request.post()
    if "mac" not in form or "json" not in form:
        return _text("Missing parameters", 400)
    mac_hex = str(form["mac"])
    mac = _mac_or_none(mac_hex)
    if mac is None:
        return _text("Missing parameters", 400)
    ctx.settings.current_dir.mkdir(parents=True, exist_ok=True)
    try:
        (ctx.settings.current_dir / f"{mac_hex}.json").write_text(str(form["json"]), encoding="utf-8")
    except OSError:
        return _text("Failed to create file", 400)
    record = ctx.db.find(mac)
    if record is None:
        return _text("mac not found in tagDB", 400)
    ttl = _int(form.get("ttl", 0))
    record.mode_config_json = json.dumps({"filename": f"/current/{mac_hex}.json", "interval": str(ttl)})
    record.content_mode = 19
    record.nextupdate = 0
    ctx.ws_send_taginfo(mac, SYNC_USERCFG)
    return _text("Ok, saved")


async def handle_backup_db(request: web.Request) -> web.StreamResponse:
    ctx = _ctx(request)
    ctx.db.save()
    return _file_response(ctx.settings.tagdb_file, download=True)


async def handle_restore_db(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    _fields, files = await _read_multipart(request)
    if not files:
        return _text("no file", 400)
    _name, data = next(iter(files.values()))
    ctx.events.log_line("restore tagDB")
    target = ctx.settings.current_dir / "tagDBrestored.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    ctx.db.records.clear()
    if not ctx.db.load(target):
        ctx.events.log_line("restore tagDB failed: file is not a valid tagDB.json")
        return _text("restore failed: not a valid tagDB.json", 400)
    ctx.events.log_line(f"restore tagDB: {len(ctx.db.records)} tag(s) restored")
    return _text("Ok, restored.")


# --------------------------------------------------------------------------
# OTA / system endpoints
# --------------------------------------------------------------------------
async def handle_sysinfo(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    cfg = ctx.db.config
    return _json({
        "alias": cfg.alias,
        "env": cfg.env,
        "buildtime": "0",
        "buildversion": __version__,
        "sha": "0",
        "psramsize": 0,
        "flashsize": 0,
        "rollback": False,
        "ap_version": ctx.ap_info.version,
        "ap_type": ctx.ap_info.type,
        "ap_typename": ap_type_name(ctx.ap_info.type),
        "has_sws_flasher": 0,
        "flashpin_defaults": {"sws": -1, "reset": ctx.settings.gpio_reset_line, "txd": -1, "rxd": -1,
                              "prog": ctx.settings.gpio_boot_line, "dbgtxd": -1, "dbgrxd": -1},
        "hasC6": 1,
        "hasH2": 0,
        "hasTslr": 0,
        "hasFlasher": 0,
        "platform": "linux",
    })


async def handle_check_file(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    if "path" not in request.query:
        raise web.HTTPBadRequest()
    path = _resolve_fs_path(ctx, request.query["path"])
    if path is None or not path.is_file():
        return _json({"filesize": 0, "md5": ""})
    data = path.read_bytes()
    return _json({"filesize": len(data), "md5": hashlib.md5(data).hexdigest()})


async def handle_rollback(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    ctx.events.ws_serial("Rollback not allowed")
    return _text("Rollback not allowed", 400)


async def handle_update_ota(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    ctx.events.ws_serial("Host firmware updates are handled by the operating system on this access point.")
    ctx.events.ws_serial("Update the Python service with git pull / pip install and restart it.")
    return _text("Not applicable on Linux host", 400)


async def handle_update_c6(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    form = await request.post()
    if "url" not in form:
        return _text("Bad request", 400)
    if ctx.flasher is None:
        return _text("C6 flashing not available", 400)
    ctx.flasher.start_update(str(form["url"]))
    return _text("Ok")


async def handle_update_actions(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    ctx.events.ws_serial("Performing cleanup")
    path = ctx.settings.data_path / "update_actions.json"
    if not path.is_file():
        ctx.events.ws_serial("No update_actions.json present")
        return _text("No update actions needed")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        doc = {}
    for file_path in doc.get("deletefile", []) if isinstance(doc, dict) else []:
        target = _write_path(ctx, str(file_path))
        if target is not None and target.is_file():
            target.unlink()
            ctx.events.ws_serial("deleted file: " + str(file_path))
    ctx.events.ws_serial("Cleanup finished")
    path.unlink(missing_ok=True)
    return _text("Clean up finished")


async def handle_littlefs_put(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    fields, files = await _read_multipart(request)
    if "path" not in fields or not files:
        return _text("Error. Disk full?", 507)
    target = _write_path(ctx, fields["path"])
    if target is None:
        return _text("invalid path", 400)
    _name, data = next(iter(files.values()))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError:
        return _text("Error. Disk full?", 507)
    if target.parent.name == "tagtypes":
        ctx.db.tagtypes.invalidate()
    return _text("Ok, file written")


async def handle_reboot(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    ctx.events.log_line("Reboot request by user")
    ctx.events.ws_err("REBOOTING")
    asyncio.get_running_loop().call_later(0.5, _request_restart, ctx)
    return _text("OK Reboot")


def _request_restart(ctx: AppContext) -> None:
    """Persist state and exit; systemd restarts the service."""
    try:
        ctx.db.config.run_status = RUNSTATUS_STOP
        if ctx.proto is not None:
            ctx.proto.refresh_all_pending_sync()
        ctx.db.save()
    finally:
        if ctx.request_shutdown is not None:
            ctx.request_shutdown()


# --------------------------------------------------------------------------
# /edit – file manager (SPIFFSEditor)
# --------------------------------------------------------------------------
def _list_dir(ctx: AppContext, path: str, recursive: bool) -> list[dict[str, Any]]:
    if recursive and path in ("//www", "//tagtypes", "//current"):
        return []
    rel = path.lstrip("/")
    base = ctx.settings.data_path / rel if rel else ctx.settings.data_path
    result: list[dict[str, Any]] = []
    if not base.is_dir():
        return result
    for item in sorted(base.iterdir()):
        if item.is_dir():
            if recursive:
                result.extend(_list_dir(ctx, path + "/" + item.name, True))
            else:
                result.append({"type": "dir", "name": item.name})
        else:
            if recursive:
                result.append({"type": "file", "name": path[2:] + "/" + item.name, "size": item.stat().st_size})
            else:
                result.append({"type": "file", "name": item.name, "size": item.stat().st_size})
    return result


async def handle_edit_get(request: web.Request) -> web.StreamResponse:
    ctx = _ctx(request)
    if "list" in request.query:
        return _json(_list_dir(ctx, request.query["list"], "recursive" in request.query))
    if "edit" in request.query or "download" in request.query:
        name = request.query.get("edit") or request.query.get("download") or ""
        path = _resolve_fs_path(ctx, "/" + name.lstrip("/"))
        if path is None or not path.is_file():
            raise web.HTTPNotFound()
        return _file_response(path, download="download" in request.query)
    page = ctx.settings.www_path / "edit.html"
    if not page.is_file():
        raise web.HTTPNotFound()
    return _file_response(page)


async def handle_edit_delete(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    form = await request.post()
    if "path" not in form:
        raise web.HTTPNotFound()
    target = _write_path(ctx, str(form["path"]))
    if target is not None and target.exists():
        if target.is_dir():
            import shutil

            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink()
    if target is not None and target.parent.name == "tagtypes":
        ctx.db.tagtypes.invalidate()
    return _text("DELETE: " + str(form["path"]))


async def handle_edit_put(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    form = await request.post()
    if "path" not in form:
        raise web.HTTPBadRequest()
    target = _write_path(ctx, str(form["path"]))
    if target is None:
        raise web.HTTPBadRequest()
    if target.exists():
        return web.Response(status=200)
    try:
        if str(form["path"]).endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x00")
    except OSError:
        raise web.HTTPInternalServerError()
    return _text("CREATE: /" + str(form["path"]).lstrip("/"))


async def handle_edit_post(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    fields, files = await _read_multipart(request)
    if not files:
        raise web.HTTPInternalServerError()
    written = ""
    for _field, (filename, data) in files.items():
        name = fields.get("path", filename)
        target = _write_path(ctx, name)
        if target is None:
            raise web.HTTPInternalServerError()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written = filename
        if target.parent.name == "tagtypes":
            ctx.db.tagtypes.invalidate()
    return _text("UPLOADED: " + written)


# --------------------------------------------------------------------------
# Middleware and app factory
# --------------------------------------------------------------------------
@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if request.method == "OPTIONS":
        response: web.StreamResponse = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "content-type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


def create_app(ctx: AppContext) -> web.Application:
    app = web.Application(middlewares=[cors_middleware], client_max_size=64 * 1024 * 1024)
    app[CTX_KEY] = ctx
    routes = [
        web.get("/", handle_index),
        web.get("/ws", handle_ws),
        web.post("/reboot", handle_reboot),
        web.get("/current/{name:.*}", handle_current),
        web.get("/tagtypes/{name:.*}", handle_tagtypes),
        web.post("/imgupload", handle_imgupload),
        web.post("/jsonupload", handle_jsonupload),
        web.get("/get_db", handle_get_db),
        web.get("/getdata", handle_getdata),
        web.post("/save_cfg", handle_save_cfg),
        web.post("/tag_cmd", handle_tag_cmd),
        web.get("/led_flash", handle_led_flash),
        web.get("/get_ap_config", handle_get_ap_config),
        web.post("/save_apcfg", handle_save_apcfg),
        web.post("/set_time", handle_set_time),
        web.post("/set_var", handle_set_var),
        web.post("/set_vars", handle_set_vars),
        web.get("/setup", handle_setup),
        web.get("/get_wifi_config", handle_get_wifi_config),
        web.get("/get_ssid_list", handle_get_ssid_list),
        web.post("/save_wifi_config", handle_save_wifi_config),
        web.get("/backup_db", handle_backup_db),
        web.post("/restore_db", handle_restore_db),
        web.get("/sysinfo", handle_sysinfo),
        web.get("/check_file", handle_check_file),
        web.post("/rollback", handle_rollback),
        web.post("/update_c6", handle_update_c6),
        web.post("/update_actions", handle_update_actions),
        web.post("/update_ota", handle_update_ota),
        web.post("/littlefs_put", handle_littlefs_put),
        web.get("/edit", handle_edit_get),
        web.delete("/edit", handle_edit_delete),
        web.put("/edit", handle_edit_put),
        web.post("/edit", handle_edit_post),
        web.get("/{name:.*}", handle_www),
    ]
    app.add_routes(routes)
    return app


async def sysinfo_task(ctx: AppContext, interval: float = 5.0) -> None:
    """Periodic ``wsSendSysteminfo`` plus the nightly restart."""
    while True:
        try:
            payload = build_sysinfo_payload(ctx)
            ctx.events.ws_sysinfo(payload)
            local = time.localtime()
            if (local.tm_hour == 3 and local.tm_min == 56 and ctx.uptime > 2 * 3600
                    and ctx.db.config.nightlyreboot == 1):
                ctx.events.log_line("Nightly reboot")
                ctx.events.ws_err("REBOOTING")
                await asyncio.sleep(5)
                _request_restart(ctx)
                return
        except Exception:
            log.exception("sysinfo task failed")
        await asyncio.sleep(interval)


def ap_is_ready(ctx: AppContext) -> bool:
    return ctx.ap_info.state in (AP_STATE_ONLINE, AP_STATE_NORADIO)
