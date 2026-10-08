"""Multi-AP synchronisation over UDP (``udp.cpp``).

Access points on the same network exchange tag status, "data available"
announcements and transfer results on multicast 239.10.0.1:16033 (or via
broadcast when ``discovery`` is set).  The wire format is the packed C
structs of the firmware prefixed with a packet type byte.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from typing import TYPE_CHECKING

from . import protocol as P
from .tagdb import TagRecord

if TYPE_CHECKING:  # pragma: no cover
    from .state import AppContext

log = logging.getLogger(__name__)

UDP_GROUP = "239.10.0.1"
UDP_PORT = 16033
SYNC_VERSION = 0xAA01
AUTOSELECT_CHANNELS = [11, 15, 20, 25, 26, 27]

# struct APlist: u32 src, char alias[32], u8 channelId, u8 tagCount, u16 version
_APLIST = struct.Struct("<I32sBBH")
# struct TagInfo (71 bytes)
_TAGINFO = struct.Struct("<H8sB32sIIHIBBBHB8s")


def pack_aplist(src_ip: str, alias: str, channel: int, tag_count: int, version: int) -> bytes:
    try:
        src = struct.unpack("<I", socket.inet_aton(src_ip))[0]
    except OSError:
        src = 0
    return _APLIST.pack(src, alias.encode("utf-8", errors="replace")[:31].ljust(32, b"\x00"),
                        channel & 0xFF, min(tag_count, 255), version & 0xFFFF)


def unpack_aplist(data: bytes) -> dict | None:
    if len(data) < _APLIST.size:
        data = data.ljust(_APLIST.size, b"\x00")
    src, alias, channel, tag_count, version = _APLIST.unpack(data[:_APLIST.size])
    return {
        "ip": socket.inet_ntoa(struct.pack("<I", src)),
        "alias": alias.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
        "count": tag_count,
        "channel": channel,
        "version": f"{version:04X}",
    }


def pack_taginfo(record: TagRecord, sync_mode: int) -> bytes:
    return _TAGINFO.pack(
        SYNC_VERSION, bytes(record.mac).ljust(8, b"\x00")[:8], sync_mode & 0xFF,
        record.alias.encode("utf-8", errors="replace")[:31].ljust(32, b"\x00"),
        record.lastseen & 0xFFFFFFFF, record.nextupdate & 0xFFFFFFFF, record.pending_count & 0xFFFF,
        record.expected_next_checkin & 0xFFFFFFFF, record.hw_type & 0xFF, record.wakeup_reason & 0xFF,
        record.capabilities & 0xFF, record.pending_idle & 0xFFFF, record.content_mode & 0xFF, b"\x00" * 8)


def unpack_taginfo(data: bytes) -> dict | None:
    if len(data) < _TAGINFO.size:
        data = data.ljust(_TAGINFO.size, b"\x00")
    fields = _TAGINFO.unpack(data[:_TAGINFO.size])
    (version, mac, sync_mode, alias, lastseen, nextupdate, pending_count, expected, hw_type, wakeup,
     capabilities, pending_idle, content_mode, _reserved) = fields
    if version != SYNC_VERSION:
        return None
    return {
        "mac": bytes(mac),
        "sync_mode": sync_mode,
        "alias": alias.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
        "lastseen": lastseen,
        "nextupdate": nextupdate,
        "pending_count": pending_count,
        "expected_next_checkin": expected,
        "hw_type": hw_type,
        "wakeup_reason": wakeup,
        "capabilities": capabilities,
        "pending_idle": pending_idle,
        "content_mode": content_mode,
    }


def local_ip_for(target: str) -> str:
    """The IPv4 address the kernel would use to reach ``target``."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((target, 9))
            return probe.getsockname()[0]
    except OSError:
        return "0.0.0.0"


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "UDPSync") -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self.owner.on_packet(data, addr[0])

    def error_received(self, exc: Exception) -> None:
        log.debug("udp error: %s", exc)


class UDPSync:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.transport: asyncio.DatagramTransport | None = None
        self.channel_list: list[int] = []
        self._autoselect_task: asyncio.Task | None = None
        self.local_ip = "0.0.0.0"

    # ------------------------------------------------------------------
    @property
    def discovery(self) -> int:
        return self.ctx.db.config.discovery

    @property
    def _channel(self) -> int:
        serial = self.ctx.serial
        return serial.cur_channel.channel if serial is not None else self.ctx.db.config.channel

    async def run(self) -> None:
        """Open the socket and keep it open; re-opens after errors."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                sock = self._make_socket()
                self.transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
                self.local_ip = local_ip_for(UDP_GROUP)
                log.info("udp sync listening on %s:%d (%s)", UDP_GROUP if self.discovery == 0 else "broadcast",
                         UDP_PORT, self.local_ip)
                while self.transport is not None and not self.transport.is_closing():
                    await asyncio.sleep(5)
                    new_ip = local_ip_for(UDP_GROUP)
                    if new_ip != self.local_ip:
                        self.local_ip = new_ip
            except asyncio.CancelledError:
                if self.transport is not None:
                    self.transport.close()
                raise
            except OSError as exc:
                log.warning("udp sync socket failed: %s (retrying in 10 s)", exc)
                self.transport = None
                await asyncio.sleep(10)

    def _make_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", UDP_PORT))
        if self.discovery == 0:
            membership = socket.inet_aton(UDP_GROUP) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        sock.setblocking(False)
        return sock

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    def _send(self, payload: bytes, target: str = UDP_GROUP) -> None:
        if self.transport is None:
            return
        try:
            if self.discovery == 0:
                self.transport.sendto(payload, (target, UDP_PORT))
            else:
                self.transport.sendto(payload, ("255.255.255.255", UDP_PORT))
        except OSError as exc:
            log.debug("udp send failed: %s", exc)

    def _aplist_item(self) -> bytes:
        tag_count, _timeout, _lowbatt = self.ctx.db.tag_count()
        return pack_aplist(self.local_ip, self.ctx.db.config.alias, self._channel, tag_count,
                           self.ctx.ap_info.version)

    def get_ap_list(self) -> None:
        """``getAPList``: show ourselves and ask the other APs to report."""
        item = self._aplist_item()
        self.ctx.events.ws_apitem(unpack_aplist(item))
        if self.ctx.db.config.channel == 0:
            # The firmware intends to pick a free channel automatically when
            # none is configured; run that selection here.
            self.start_autoselect()
        self._send(bytes([P.PKT_APLIST_REQ]) + item)

    def net_process_data_req(self, eadr: P.EspAvailDataReq) -> None:
        self._send(bytes([P.PKT_AVAIL_DATA_INFO]) + eadr.pack())

    def net_process_xfer_complete(self, xfc: P.XferComplete) -> None:
        self._send(bytes([P.PKT_XFER_COMPLETE]) + xfc.pack())

    def net_process_xfer_timeout(self, xfc: P.XferComplete) -> None:
        self._send(bytes([P.PKT_XFER_TIMEOUT]) + xfc.pack())

    def net_send_data_avail(self, pending: P.PendingData) -> None:
        self._send(bytes([P.PKT_AVAIL_DATA_REQ]) + pending.pack())

    def net_taginfo(self, record: TagRecord, sync_mode: int) -> None:
        self._send(bytes([P.PKT_TAGINFO]) + pack_taginfo(record, sync_mode))

    # ------------------------------------------------------------------
    # receiving
    # ------------------------------------------------------------------
    def on_packet(self, data: bytes, sender_ip: str) -> None:
        if not data or sender_ip == self.local_ip:
            return
        if self.ctx.db.config.run_status == P.RUNSTATUS_STOP:
            return
        proto = self.ctx.proto
        kind = data[0]
        payload = data[1:]
        loop = asyncio.get_running_loop()
        try:
            if kind == P.PKT_AVAIL_DATA_INFO and proto is not None:
                eadr = P.EspAvailDataReq.unpack(payload.ljust(P.ESP_AVAIL_DATA_REQ_SIZE, b"\x00"))
                loop.create_task(proto.process_data_req(eadr, False, sender_ip))
            elif kind == P.PKT_XFER_COMPLETE and proto is not None:
                xfc = P.XferComplete.unpack(payload.ljust(P.XFER_COMPLETE_SIZE, b"\x00"))
                loop.create_task(proto.process_xfer_complete(xfc, False))
            elif kind == P.PKT_XFER_TIMEOUT and proto is not None:
                xfc = P.XferComplete.unpack(payload.ljust(P.XFER_COMPLETE_SIZE, b"\x00"))
                loop.create_task(proto.process_xfer_timeout(xfc, False))
            elif kind == P.PKT_AVAIL_DATA_REQ and proto is not None:
                pending = P.PendingData.unpack(payload.ljust(P.PENDING_DATA_SIZE, b"\x00"))
                loop.create_task(proto.prepare_external_data_avail(pending, sender_ip))
            elif kind == P.PKT_APLIST_REQ:
                self._send(bytes([P.PKT_APLIST_REPLY]) + self._aplist_item(), sender_ip)
            elif kind == P.PKT_APLIST_REPLY:
                reply = unpack_aplist(payload)
                if reply is not None:
                    self.channel_list = [0 if ch == reply["channel"] else ch for ch in self.channel_list]
                    self.ctx.events.ws_apitem(reply)
            elif kind == P.PKT_TAGINFO:
                if len(payload) < 2 or struct.unpack("<H", payload[:2])[0] != SYNC_VERSION:
                    self.ctx.events.ws_err(f"Got a packet from {sender_ip} with mismatched udp sync version. "
                                           "Update firmware!")
                else:
                    item = unpack_taginfo(payload)
                    if item is not None and proto is not None:
                        proto.update_taginfo_item(item, sender_ip)
        except Exception:  # noqa: BLE001 - malformed packets must not kill the listener
            log.exception("bad udp packet from %s", sender_ip)

    # ------------------------------------------------------------------
    # channel autoselect
    # ------------------------------------------------------------------
    def start_autoselect(self) -> None:
        if self._autoselect_task is not None and not self._autoselect_task.done():
            return
        self._autoselect_task = asyncio.get_running_loop().create_task(self._autoselect(), name="autoselect")

    async def _autoselect(self) -> None:
        """``autoselect``: pick the first channel no other AP reported within 5 s."""
        self.channel_list = list(AUTOSELECT_CHANNELS)
        await asyncio.sleep(5)
        channel = next((ch for ch in self.channel_list if ch > 0), 0) or 11
        self.ctx.db.config.channel = channel
        serial = self.ctx.serial
        if serial is not None:
            serial.cur_channel.channel = channel
        while not self.ctx.ap_info.is_online:
            await asyncio.sleep(1)
        if serial is not None:
            await serial.send_channel_power(serial.cur_channel)
        self.ctx.db.save_config()
        log.info("autoselected channel %d", channel)
