"""Shared runtime state: ``apInfo`` and the application context.

The :class:`AppContext` is the Python equivalent of the global variables of
the ESP firmware.  Subsystems are attached as attributes so that web
handlers, the serial driver and the content manager can reach each other
without import cycles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from .config import Settings
from .events import Events, WebSocketHub
from .language import Language
from .protocol import AP_STATE_OFFLINE
from .tagdb import TagDB

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .content.manager import ContentManager
    from .flasher import C6Flasher
    from .leds import RgbLed
    from .newproto import NewProto
    from .serialap import SerialAP
    from .udp import UDPSync

ESP32_C6 = 0x05
TLSR_ZIGBEE_AP = 0xF8
TLSR_GFSK_AP = 0xF9


def ap_type_name(ap_type: int) -> str:
    return {
        ESP32_C6: "ESP32-C6",
        TLSR_ZIGBEE_AP: "TLSR Zigbee 802.15.4",
        TLSR_GFSK_AP: "TLSR GFSK",
    }.get(ap_type, "unknown")


@dataclass
class APInfo:
    """``struct APInfoS`` – what we know about the radio co-processor."""

    is_online: bool = False
    state: int = AP_STATE_OFFLINE
    type: int = 0
    version: int = 0
    channel: int = 0
    mac: bytes = b"\x00" * 8
    power: int = 0
    pending_buffer: int = 0
    nop: int = 0
    has_subghz: bool = False
    subghz_channel: int = 0


@dataclass
class AppContext:
    settings: Settings
    db: TagDB
    hub: WebSocketHub
    events: Events
    language: Language
    ap_info: APInfo = field(default_factory=APInfo)
    started_at: float = field(default_factory=time.monotonic)

    serial: Optional["SerialAP"] = None
    proto: Optional["NewProto"] = None
    content: Optional["ContentManager"] = None
    flasher: Optional["C6Flasher"] = None
    udp: Optional["UDPSync"] = None
    leds: Optional["RgbLed"] = None

    # Set by the main module; invoked by /reboot
    request_shutdown: Any = None

    @property
    def uptime(self) -> int:
        return int(time.monotonic() - self.started_at)

    @property
    def millis(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def ws_send_taginfo(self, mac: bytes, sync_mode: int = 0) -> None:
        """``wsSendTaginfo``: push the tag to the UI and optionally to other APs."""
        from .protocol import SYNC_DELETE, SYNC_NOSYNC

        if sync_mode != SYNC_DELETE:
            payload = self.db.to_json(mac)
            self.hub.broadcast(payload)
        if sync_mode > SYNC_NOSYNC and self.udp is not None:
            record = self.db.find(mac)
            if record is not None and (record.content_mode != 12 or sync_mode == SYNC_DELETE):
                self.udp.net_taginfo(record, sync_mode)

    @classmethod
    def create(cls, settings: Settings) -> "AppContext":
        hub = WebSocketHub()
        events = Events(hub, settings.log_file)
        db = TagDB(settings)
        language = Language(settings.languages_file, db.config.language)
        return cls(settings=settings, db=db, hub=hub, events=events, language=language)
