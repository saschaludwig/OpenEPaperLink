"""Tag database, access point configuration, variables and tag types.

The on-disk formats are identical to the ESP firmware (``tag_db.cpp``):

* ``<data>/current/tagDB.json`` – JSON array of tag records
* ``<data>/current/apconfig.json`` – access point settings
* ``<data>/tagtypes/XX.json`` – hardware descriptions
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .protocol import RUNSTATUS_INIT, hex_to_mac, mac_to_hex

log = logging.getLogger(__name__)

BPP_PACKED_2BIT = 5


# --------------------------------------------------------------------------
# Tag record
# --------------------------------------------------------------------------
@dataclass
class TagRecord:
    mac: bytes = b"\x00" * 8
    version: int = 0
    alias: str = ""
    lastseen: int = 0
    nextupdate: int = 0
    content_mode: int = 0
    pending_count: int = 0
    md5: bytes = b"\x00" * 16
    expected_next_checkin: int = 0
    mode_config_json: str = ""
    lqi: int = 0
    rssi: int = 0
    temperature: int = 0
    battery_mv: int = 0
    hw_type: int = 0
    wakeup_reason: int = 0
    capabilities: int = 0
    last_full_update: int = 0
    is_external: bool = False
    ap_ip: str = "0.0.0.0"
    pending_idle: int = 0
    rotate: int = 0
    lut: int = 0
    tag_software_version: int = 0
    current_channel: int = 0
    invert: int = 0
    update_count: int = 0
    update_last: int = 0

    # pending transfer
    data_type: int = 0
    filename: str = ""
    data: bytes | None = None

    @property
    def mac_hex(self) -> str:
        return mac_to_hex(self.mac)

    @property
    def mode_config(self) -> dict[str, Any]:
        if not self.mode_config_json:
            return {}
        try:
            value = json.loads(self.mode_config_json)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    def to_json(self) -> dict[str, Any]:
        """Serialise into the dictionary produced by ``fillNode``."""
        return {
            "mac": self.mac_hex,
            "hash": self.md5.hex(),
            "lastseen": self.lastseen,
            "nextupdate": self.nextupdate,
            "nextcheckin": self.expected_next_checkin,
            "pending": self.pending_count,
            "alias": self.alias,
            "contentMode": self.content_mode,
            "LQI": self.lqi,
            "RSSI": self.rssi,
            "temperature": self.temperature,
            "batteryMv": self.battery_mv,
            "hwType": self.hw_type,
            "wakeupReason": self.wakeup_reason,
            "capabilities": self.capabilities,
            "modecfgjson": self.mode_config_json,
            "isexternal": self.is_external,
            "apip": self.ap_ip,
            "rotate": self.rotate,
            "lut": self.lut,
            "invert": self.invert,
            "updatecount": self.update_count,
            "updatelast": self.update_last,
            "ch": self.current_channel,
            "ver": self.tag_software_version,
        }

    @classmethod
    def from_json(cls, tag: dict[str, Any], now: int | None = None) -> "TagRecord | None":
        try:
            mac = hex_to_mac(str(tag.get("mac", "")))
        except ValueError:
            return None
        now = int(now if now is not None else time.time())
        record = cls(mac=mac)
        record.apply_json(tag, now)
        return record

    def apply_json(self, tag: dict[str, Any], now: int) -> None:
        md5 = str(tag.get("hash", ""))
        if len(md5) >= 32:
            try:
                self.md5 = bytes.fromhex(md5[:32])
            except ValueError:
                pass
        self.lastseen = int(tag.get("lastseen", 0) or 0)
        self.nextupdate = int(tag.get("nextupdate", 0) or 0)
        self.expected_next_checkin = int(tag.get("nextcheckin", 0) or 0)
        if self.expected_next_checkin < now:
            self.expected_next_checkin = now + 60
        self.pending_count = 0
        self.alias = str(tag.get("alias", "") or "")
        self.content_mode = int(tag.get("contentMode", 0) or 0)
        self.lqi = int(tag.get("LQI", 0) or 0)
        self.rssi = int(tag.get("RSSI", 0) or 0)
        self.temperature = int(tag.get("temperature", 0) or 0)
        self.battery_mv = int(tag.get("batteryMv", 0) or 0)
        self.hw_type = int(tag.get("hwType", 0) or 0) & 0xFF
        self.wakeup_reason = int(tag.get("wakeupReason", 0) or 0)
        self.capabilities = int(tag.get("capabilities", 0) or 0)
        self.mode_config_json = str(tag.get("modecfgjson", "") or "")
        self.is_external = bool(tag.get("isexternal", False))
        self.ap_ip = str(tag.get("apip", "0.0.0.0") or "0.0.0.0")
        self.rotate = int(tag.get("rotate", 0) or 0)
        self.lut = int(tag.get("lut", 0) or 0)
        self.invert = int(tag.get("invert", 0) or 0)
        self.update_count = int(tag.get("updatecount", 0) or 0)
        self.update_last = int(tag.get("updatelast", 0) or 0)
        self.current_channel = int(tag.get("ch", 0) or 0)
        self.tag_software_version = int(tag.get("ver", 0) or 0)


# --------------------------------------------------------------------------
# Access point configuration (``struct Config``)
# --------------------------------------------------------------------------
@dataclass
class APConfig:
    channel: int = 0
    subghzchannel: int = 0
    alias: str = ""
    led: int = 255
    tft: int = 255
    language: int = 0
    maxsleep: int = 10
    stopsleep: int = 1
    run_status: int = RUNSTATUS_INIT
    preview: int = 1
    nightlyreboot: int = 1
    lock: int = 0
    wifipower: int = 34
    timezone: str = "CET-1CEST,M3.5.0,M10.5.0/3"
    sleeptime1: int = 0
    sleeptime2: int = 0
    ble: int = 0
    discovery: int = 0
    showtimestamp: int = 0
    repo: str = "OpenEPaperLink/OpenEPaperLink"
    env: str = "LINUX_NANOPI_NEO"

    _INT_FIELDS = ("channel", "subghzchannel", "led", "tft", "language", "maxsleep", "stopsleep", "preview",
                   "nightlyreboot", "lock", "wifipower", "sleeptime1", "sleeptime2", "ble", "discovery",
                   "showtimestamp")

    @classmethod
    def load(cls, path: Path) -> "APConfig":
        cfg = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("failed to read %s, using defaults: %s", path, exc)
                data = {}
            for name in cls._INT_FIELDS:
                if isinstance(data.get(name), int):
                    setattr(cfg, name, data[name])
            for name in ("alias", "timezone", "repo", "env"):
                if isinstance(data.get(name), str) and data[name]:
                    setattr(cfg, name, data[name])
        return cfg

    def to_json(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in self._INT_FIELDS}
        data.update({"alias": self.alias, "timezone": self.timezone, "repo": self.repo, "env": self.env})
        return data

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, path)


# --------------------------------------------------------------------------
# Hardware type (``struct HwType``)
# --------------------------------------------------------------------------
@dataclass
class HwType:
    id: int = 0
    width: int = 0
    height: int = 0
    rotatebuffer: int = 0
    bpp: int = 0
    shortlut: int = 0
    zlib: int = 0
    g5: int = 0
    highlight_color: int = 2
    colortable: list[tuple[int, int, int]] = field(default_factory=list)
    name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.width > 0 and self.height > 0


def _hex_flag(value: Any) -> int:
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return 0
    return 0


class TagTypeStore:
    """Load ``tagtypes/XX.json`` with template inheritance and cache the result."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._cache: dict[int, HwType] = {}
        self._json_cache: dict[int, dict[str, Any]] = {}

    def invalidate(self) -> None:
        self._cache.clear()
        self._json_cache.clear()

    def seed(self) -> None:
        """Copy the repository tag types to the data directory on first start."""
        target = self._settings.tagtypes_dir
        source = Path(self._settings.tagtypes_seed_dir)
        if not source.is_dir():
            return
        target.mkdir(parents=True, exist_ok=True)
        for item in source.glob("*.json"):
            dest = target / item.name
            if not dest.exists():
                shutil.copyfile(item, dest)

    def path_for(self, hw_type: int) -> Path:
        return self._settings.tagtypes_dir / f"{hw_type:02X}.json"

    def load_json(self, hw_type: int) -> dict[str, Any]:
        """Return the raw tag type JSON document."""
        if hw_type in self._json_cache:
            return self._json_cache[hw_type]
        path = self.path_for(hw_type)
        if not path.exists():
            seed = Path(self._settings.tagtypes_seed_dir) / f"{hw_type:02X}.json"
            path = seed
        if not path.exists():
            return {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("json error in %s: %s", path, exc)
            return {}
        if not isinstance(doc, dict):
            return {}
        self._json_cache[hw_type] = doc
        return doc

    def get_template(self, content_id: int, hw_type: int, _depth: int = 0) -> Any:
        """Return ``template[content_id]`` of a tag type (``getTemplate``).

        ``usetemplate`` redirects to the template section of another type.
        """
        if _depth > 8:
            return None
        doc = self.load_json(hw_type)
        template = doc.get("template")
        if isinstance(template, dict) and str(content_id) in template:
            return copy.deepcopy(template[str(content_id)])
        use = doc.get("usetemplate")
        if isinstance(use, int) and use != hw_type:
            return self.get_template(content_id, use, _depth + 1)
        return None

    def get(self, hw_type: int) -> HwType:
        if hw_type in self._cache:
            return self._cache[hw_type]
        doc = self.load_json(hw_type)
        hw = HwType(id=hw_type)
        if doc:
            hw.width = int(doc.get("width", 0) or 0)
            hw.height = int(doc.get("height", 0) or 0)
            hw.rotatebuffer = int(doc.get("rotatebuffer", 0) or 0)
            hw.bpp = int(doc.get("bpp", 0) or 0)
            hw.shortlut = int(doc.get("shortlut", 0) or 0)
            hw.zlib = _hex_flag(doc.get("zlib_compression"))
            hw.g5 = _hex_flag(doc.get("g5_compression"))
            hl = doc.get("highlight_color", 2)
            hw.highlight_color = int(hl) if isinstance(hl, int) else 2
            colortable = doc.get("colortable", {})
            if isinstance(colortable, dict):
                for _name, rgb in colortable.items():
                    if isinstance(rgb, list) and len(rgb) >= 3:
                        hw.colortable.append((int(rgb[0]), int(rgb[1]), int(rgb[2])))
            hw.name = str(doc.get("name", ""))
            hw.raw = doc
            self._cache[hw_type] = hw
        return hw


# --------------------------------------------------------------------------
# Variable store (``varDB``)
# --------------------------------------------------------------------------
@dataclass
class VarEntry:
    value: str
    changed: bool


class VarDB:
    def __init__(self) -> None:
        self._vars: dict[str, VarEntry] = {}

    def set(self, key: str, value: str, notify: bool = True) -> bool:
        entry = self._vars.get(key)
        if entry is None:
            self._vars[key] = VarEntry(value, notify)
            return True
        if entry.value != value:
            entry.value = value
            entry.changed = notify
            return True
        return False

    def get(self, key: str, default: str = "") -> str:
        entry = self._vars.get(key)
        return entry.value if entry else default

    def items(self) -> Iterable[tuple[str, VarEntry]]:
        return self._vars.items()

    def __contains__(self, key: str) -> bool:
        return key in self._vars


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
class TagDB:
    """In-memory list of :class:`TagRecord` with JSON persistence."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.records: list[TagRecord] = []
        self.config = APConfig.load(settings.apconfig_file)
        self.tagtypes = TagTypeStore(settings)
        self.vars = VarDB()

    # -- lookup -----------------------------------------------------------
    def find(self, mac: bytes) -> TagRecord | None:
        for record in self.records:
            if record.mac == mac and record.version == 0:
                return record
        return None

    def find_hex(self, mac_hex: str) -> TagRecord | None:
        try:
            return self.find(hex_to_mac(mac_hex))
        except ValueError:
            return None

    def get_or_create(self, mac: bytes) -> TagRecord:
        record = self.find(mac)
        if record is None:
            record = TagRecord(mac=bytes(mac))
            self.records.append(record)
        return record

    def delete(self, mac: bytes, all_versions: bool = True) -> bool:
        for index, record in enumerate(self.records):
            if record.mac == mac and (all_versions or record.version == 0):
                del self.records[index]
                return True
        return False

    def push_taginfo(self, record: TagRecord) -> None:
        """Keep a copy of the record (``pushTagInfo``) for temporary modes."""
        clone = copy.copy(record)
        clone.version = 1
        clone.data = None
        self.records.append(clone)

    def pop_taginfo(self, mac: bytes) -> None:
        for record in list(self.records):
            if record.mac == mac and record.version == 1:
                self.delete(mac, all_versions=False)
                record.version = 0

    # -- JSON API -----------------------------------------------------------
    def to_json(self, mac: bytes | None = None, start_pos: int = 0) -> dict[str, Any]:
        """Produce the ``get_db`` response, chunked at roughly 5000 bytes."""
        doc: dict[str, Any] = {"tags": []}
        size = 20
        for index in range(start_pos, len(self.records)):
            record = self.records[index]
            select = mac is None or record.mac == mac
            if select and record.version == 0:
                node = record.to_json()
                doc["tags"].append(node)
                size += len(json.dumps(node, separators=(",", ":")))
                if size > 5000:
                    doc["continu"] = index + 1
                    break
                if mac is not None:
                    break
        return doc

    # -- persistence ------------------------------------------------------
    def save(self) -> None:
        path = self.settings.tagdb_file
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                os.replace(path, path.with_suffix(".json.bak"))
            except OSError as exc:
                log.error("error renaming tagDB to .bak: %s", exc)
        # The ESP firmware wraps every tag in its own single-element array
        # (``[[{...}],[{...}]]``) because ``saveDB`` serialises one
        # ``JsonDocument`` per record.  Write the same layout so a database
        # can be moved between a Linux AP and an ESP32 AP in both directions.
        payload = [[record.to_json()] for record in self.records if record.version == 0]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _iter_tag_nodes(payload: Any) -> Iterable[dict[str, Any]]:
        """Yield tag dictionaries from either supported on-disk layout.

        Accepts the ESP firmware format ``[[{tag}], [{tag}], ...]`` as well as
        the flat format ``[{tag}, {tag}, ...]`` written by older Linux AP
        versions.
        """
        if not isinstance(payload, list):
            return
        for item in payload:
            if isinstance(item, dict):
                yield item
            elif isinstance(item, list):
                for tag in item:
                    if isinstance(tag, dict):
                        yield tag

    def load(self, path: Path | None = None) -> bool:
        path = path or self.settings.tagdb_file
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("loadDB failed: %s", exc)
            return False
        if not isinstance(payload, list):
            log.error("loadDB failed: %s does not contain a JSON array", path)
            return False
        now = int(time.time())
        loaded = 0
        for tag in self._iter_tag_nodes(payload):
            try:
                mac = hex_to_mac(str(tag.get("mac", "")))
            except ValueError:
                continue
            record = self.find(mac)
            if record is None:
                record = TagRecord(mac=mac)
                self.records.append(record)
            record.apply_json(tag, now)
            loaded += 1
        log.info("loadDB: %d tag(s) loaded from %s", loaded, path)
        return True

    def save_config(self) -> None:
        self.config.save(self.settings.apconfig_file)

    # -- statistics -------------------------------------------------------
    def tag_count(self) -> tuple[int, int, int]:
        """Return (tagcount, timeoutcount, lowbattcount) like ``getTagCount``."""
        now = int(time.time())
        tagcount = timeoutcount = lowbatt = 0
        for record in self.records:
            if not record.is_external:
                tagcount += 1
            timeout = now - record.lastseen
            if record.expected_next_checkin < 3600:
                if timeout > self.config.maxsleep * 60 + 300:
                    timeoutcount += 1
            elif now - record.expected_next_checkin > 600:
                if timeout > self.config.maxsleep * 60 + 300:
                    timeoutcount += 1
            if record.battery_mv < 2400 and record.battery_mv not in (0, 1337):
                lowbatt += 1
        return tagcount, timeoutcount, lowbatt

    # -- housekeeping -----------------------------------------------------
    def cleanup_current(self) -> None:
        """Delete preview files of unknown tags and stale ``.pending`` files."""
        current = self.settings.current_dir
        if not current.is_dir():
            return
        known = {record.mac for record in self.records}
        for item in current.iterdir():
            if not item.is_file():
                continue
            base = item.name[:16]
            try:
                mac = hex_to_mac(base)
            except ValueError:
                continue
            if mac not in known or item.name.endswith(".pending"):
                log.info("remove %s", item)
                try:
                    item.unlink()
                except OSError:
                    pass
        temp = self.settings.data_path / "temp"
        if temp.is_dir():
            for item in temp.iterdir():
                try:
                    item.unlink()
                except OSError:
                    pass

    def clear_pending(self, record: TagRecord) -> None:
        record.filename = ""
        record.data = None
