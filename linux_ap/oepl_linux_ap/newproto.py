"""Pending queue, block transfer and tag check-in handling (``newproto.cpp``).

The flow for a new image is:

1. :meth:`NewProto.prepare_data_avail_file` renames the generated file to
   ``/current/<MAC>_<n>.pending``, queues a :class:`PendingItem` and sends
   ``SDA>`` to the C6.
2. The tag checks in, the C6 forwards ``RQB>`` block requests which are
   served from the queued data with ``>D>`` transfers.
3. ``XFC>`` finishes the transfer: the file becomes ``/current/<MAC>.raw``
   (preview) and the next queue item, if any, is announced.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Optional

from . import protocol as P
from .state import AppContext
from .storage import ContentFS
from .tagdb import TagRecord

log = logging.getLogger(__name__)


@dataclass
class PendingItem:
    pending: P.PendingData
    filename: str = ""
    data: Optional[bytes] = None
    length: int = 0

    @property
    def mac(self) -> bytes:
        return self.pending.target_mac

    @property
    def data_ver(self) -> int:
        return self.pending.avail.data_ver


def md5_to_data_ver(md5: bytes) -> int:
    return int.from_bytes(md5[:8], "little")


def data_ver_to_md5(data_ver: int) -> bytes:
    return data_ver.to_bytes(8, "little") + b"\x00" * 8


class NewProto:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.db = ctx.db
        self.events = ctx.events
        self.fs = ContentFS(ctx.settings)
        self.queue: list[PendingItem] = []
        self._serial = ctx.serial
        if self._serial is not None:
            self._serial.proto = self

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @property
    def serial(self):
        return self.ctx.serial

    @property
    def udp(self):
        return self.ctx.udp

    def _ws_taginfo(self, mac: bytes, sync_mode: int) -> None:
        self.ctx.ws_send_taginfo(mac, sync_mode)

    def _current_path(self, mac: bytes, suffix: str) -> str:
        return f"/current/{P.mac_to_hex(mac)}{suffix}"

    def _pending_path(self, mac: bytes) -> str:
        return f"/current/{P.mac_to_hex(mac)}_{self.ctx.millis % 1000000}.pending"

    def load_item_data(self, item: PendingItem) -> bytes | None:
        if item.data is None and item.filename:
            item.data = self.fs.read_bytes(item.filename)
            if item.data is not None:
                item.length = len(item.data)
        return item.data

    def load_record_data(self, record: TagRecord) -> bytes | None:
        if record.data is None and record.filename:
            record.data = self.fs.read_bytes(record.filename)
        return record.data

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------
    def enqueue_item(self, item: PendingItem) -> None:
        self.queue.append(item)

    def dequeue_item(self, mac: bytes, data_ver: int = 0) -> bool:
        for index, item in enumerate(self.queue):
            if item.mac == mac and (data_ver == 0 or data_ver == item.data_ver):
                del self.queue[index]
                return True
        return False

    def count_queue_item(self, mac: bytes) -> int:
        return sum(1 for item in self.queue if item.mac == mac)

    def get_queue_item(self, mac: bytes, data_ver: int = 0) -> PendingItem | None:
        for item in self.queue:
            if item.mac == mac and (data_ver == 0 or data_ver == item.data_ver):
                return item
        return None

    async def check_queue(self, mac: bytes) -> None:
        count = self.count_queue_item(mac)
        if count > 0:
            item = self.get_queue_item(mac)
            if item is None:
                return
            if count > 1:
                item.pending.avail.next_check_in = 5 | 0x8000
            if self.serial is not None:
                await self.serial.send_data_avail(item.pending)

    async def queue_data_avail(self, pending: P.PendingData, local: bool) -> bool:
        record = self.db.find(pending.target_mac)
        if record is None:
            return False
        item = PendingItem(pending=P.PendingData(
            avail=P.AvailDataInfo(**pending.avail.__dict__), attempts_left=pending.attempts_left,
            target_mac=bytes(pending.target_mac)))
        item.filename = record.filename
        if record.data is not None:
            item.data = record.data
            record.data = None
        elif len(self.queue) < 5 and item.filename:
            item.data = self.fs.read_bytes(item.filename)
            if item.data is None:
                log.warning("Warning: not found: %s", item.filename)
        item.length = len(item.data) if item.data is not None else self.fs.size(item.filename)
        # ``record.len`` is tracked implicitly through the data / file size.

        data_type = pending.avail.data_type
        if data_type not in (P.DATATYPE_FW_UPDATE, P.DATATYPE_NOUPDATE) and \
                (pending.avail.data_type_argument & 0xF8) == 0:
            # A new image without preload replaces images already queued for the tag.
            self.queue = [
                other for other in self.queue
                if not (other.mac == pending.target_mac
                        and other.pending.avail.data_type == data_type
                        and (other.pending.avail.data_type_argument & 0xF8) == 0)
            ]
        self.enqueue_item(item)
        record.pending_count = self.count_queue_item(pending.target_mac)
        if local:
            await self.check_queue(pending.target_mac)
        return True

    # ------------------------------------------------------------------
    # prepare* family
    # ------------------------------------------------------------------
    async def prepare_cancel_pending(self, mac: bytes) -> None:
        pending = P.PendingData(target_mac=bytes(mac))
        if self.serial is not None:
            await self.serial.send_cancel_pending(pending)
        record = self.db.find(mac)
        if record is None:
            if not self.db.config.lock:
                self.events.ws_err("Tag not found, this shouldn't happen.")
            return
        self.db.clear_pending(record)
        while self.dequeue_item(mac):
            pass
        record.pending_count = self.count_queue_item(mac)
        self._ws_taginfo(mac, P.SYNC_TAGSTATUS)

    async def prepare_idle_req(self, mac: bytes, next_checkin: int) -> None:
        if next_checkin > 0 and self.count_queue_item(mac) == 0:
            pending = P.PendingData(
                avail=P.AvailDataInfo(data_type=P.DATATYPE_NOUPDATE, next_check_in=next_checkin),
                attempts_left=10 + self.db.config.maxsleep, target_mac=bytes(mac))
            log.info(">SDA %s sleeping %d minutes", P.mac_to_hex(mac), next_checkin)
            if self.serial is not None:
                await self.serial.send_data_avail(pending)

    async def prepare_data_avail_bytes(self, data: bytes, data_type: int, mac: bytes) -> None:
        """Queue an in-memory payload (NFC, tag config, time data, ...)."""
        record = self.db.find(mac)
        if record is None:
            if not self.db.config.lock:
                self.events.ws_err("Tag not found, this shouldn't happen.")
            return
        self.db.clear_pending(record)
        record.data = bytes(data)
        record.pending_count += 1
        record.pending_idle = 0
        record.filename = ""
        record.data_type = data_type
        md5 = hashlib.md5(data).digest()
        pending = P.PendingData(
            avail=P.AvailDataInfo(data_ver=md5_to_data_ver(md5), data_size=len(data), data_type=data_type,
                                  next_check_in=5 | 0x8000),
            attempts_left=10, target_mac=bytes(mac))
        await self.queue_data_avail(pending, not record.is_external)
        if record.is_external and self.udp is not None:
            self.udp.net_send_data_avail(pending)
        self._ws_taginfo(mac, P.SYNC_TAGSTATUS)

    async def prepare_data_avail_file(self, filename: str, data_type: int, data_type_argument: int, mac: bytes,
                                      next_checkin: int, resend: bool = False) -> bool:
        """Queue a file for a tag (image or firmware).  Returns False on error."""
        cfg = self.db.config
        if (next_checkin & 0x8000) == 0 and next_checkin > cfg.maxsleep:
            next_checkin = cfg.maxsleep
        if (next_checkin & 0x8000) == 0 and self.ctx.hub.client_count and cfg.stopsleep == 1:
            next_checkin = 0

        record = self.db.find(mac)
        if record is None:
            if not cfg.lock:
                self.events.ws_err("Tag not found, this shouldn't happen.")
            return True

        filename = ContentFS.normalise(filename)
        if not self.fs.exists(filename):
            self.events.ws_err("File not found. " + filename)
            return False
        filesize = self.fs.size(filename)
        if filesize == 0:
            self.events.ws_err("File has size 0. " + filename)
            return False
        md5 = self.fs.md5(filename) or b"\x00" * 16

        if md5[:8] == record.md5[:8]:
            self.events.ws_log("new image is the same as current image. not updating tag.")
            self._ws_taginfo(mac, P.SYNC_TAGSTATUS)
            if not resend:
                self.fs.remove(filename)
            return True

        if data_type != P.DATATYPE_FW_UPDATE:
            dst_path = self._pending_path(mac)
            if self.fs.exists(dst_path):
                self.fs.remove(dst_path)
            if not resend:
                self.fs.rename(filename, dst_path)
                filename = dst_path
                self.events.ws_log("new image: " + filename)
            record.pending_idle = ((next_checkin & 0x7FFF) + 5) if (next_checkin & 0x8000) \
                else (next_checkin * 60 + 60)
            self.db.clear_pending(record)
        else:
            self.events.ws_log("firmware upload pending")
            self.db.clear_pending(record)

        record.filename = filename
        record.data_type = data_type
        record.pending_count += 1

        pending = P.PendingData(
            avail=P.AvailDataInfo(data_ver=md5_to_data_ver(md5), data_size=filesize, data_type=data_type,
                                  data_type_argument=data_type_argument, next_check_in=next_checkin),
            attempts_left=P.MAX_XFER_ATTEMPTS, target_mac=bytes(mac))
        await self.check_mirror(record, pending)
        await self.queue_data_avail(pending, not record.is_external)
        if not record.is_external:
            log.info(">SDA %s TYPE 0x%02X", P.mac_to_hex(mac), data_type)
        elif self.udp is not None:
            self.udp.net_send_data_avail(pending)
        self._ws_taginfo(mac, P.SYNC_TAGSTATUS)
        return True

    async def prepare_external_data_avail(self, pending: P.PendingData, remote_ip: str) -> None:
        """Another AP announced data for one of *our* tags: fetch it over HTTP."""
        record = self.db.find(pending.target_mac)
        if record is None or record.is_external:
            return
        data_type = pending.avail.data_type
        hexmac = P.mac_to_hex(pending.target_mac)
        md5hex = P.mac_to_hex(pending.avail.data_ver.to_bytes(8, "little"))
        if data_type in (P.DATATYPE_IMG_ZLIB, P.DATATYPE_IMG_RAW_1BPP, P.DATATYPE_IMG_RAW_2BPP, P.DATATYPE_IMG_G5,
                         P.DATATYPE_IMG_RAW_3BPP, P.DATATYPE_IMG_RAW_4BPP):
            filename = self._pending_path(pending.target_mac)
            url = f"http://{remote_ip}/getdata?mac={hexmac}&md5={md5hex}"
            self.events.ws_log("prepareExternalDataAvail GET " + url)
            data = await _http_get_bytes(url)
            if data is None:
                data = await _http_get_bytes(f"http://{remote_ip}/current/{hexmac}.raw")
            if not data:
                self.events.ws_err("Remote file not found. " + filename)
                return
            self.fs.write_bytes(filename, data)
            self.db.clear_pending(record)
            record.filename = filename
            record.data_type = data_type
            record.pending_count += 1
        elif data_type in (P.DATATYPE_NFC_RAW_CONTENT, P.DATATYPE_NFC_URL_DIRECT):
            url = f"http://{remote_ip}/getdata?mac={hexmac}&md5={md5hex}"
            self.events.ws_log("GET " + url)
            data = await _http_get_bytes(url)
            if data:
                self.db.clear_pending(record)
                record.data = data
                record.data_type = data_type
                record.pending_count += 1
        elif data_type == P.DATATYPE_FW_UPDATE:
            return
        await self.check_mirror(record, pending)
        await self.queue_data_avail(pending, not record.is_external)
        self._ws_taginfo(pending.target_mac, P.SYNC_NOSYNC)

    async def check_mirror(self, record: TagRecord, pending: P.PendingData) -> None:
        """Content mode 20 ("mirror") copies the pending image to other tags."""
        for other in list(self.db.records):
            if other.content_mode != 20 or other.version != 0:
                continue
            cfg = other.mode_config
            try:
                mirror_mac = P.hex_to_mac(str(cfg.get("mac", "")))
            except ValueError:
                continue
            if mirror_mac != record.mac:
                continue
            data = self.load_record_data(record)
            if data is None:
                return
            self.db.clear_pending(other)
            other.expected_next_checkin = record.expected_next_checkin
            other.filename = record.filename
            other.data = data
            other.data_type = record.data_type
            other.pending_count += 1
            other.nextupdate = P.NO_UPDATE_SENTINEL
            pending2 = P.PendingData(
                avail=P.AvailDataInfo(data_ver=pending.avail.data_ver, data_size=len(data),
                                      data_type=other.data_type,
                                      data_type_argument=pending.avail.data_type_argument,
                                      next_check_in=pending.avail.next_check_in),
                attempts_left=pending.attempts_left, target_mac=bytes(other.mac))
            if not other.is_external:
                await self.queue_data_avail(pending2, True)
            else:
                dst_path = self._pending_path(other.mac)
                if self.fs.write_bytes(dst_path, data):
                    await self.queue_data_avail(pending2, False)
                    if self.udp is not None:
                        self.udp.net_send_data_avail(pending2)
            self._ws_taginfo(other.mac, P.SYNC_TAGSTATUS)

    # ------------------------------------------------------------------
    # incoming from the radio
    # ------------------------------------------------------------------
    async def process_block_request(self, br: P.BlockRequest, raw: bytes) -> None:
        if self.db.config.run_status == P.RUNSTATUS_STOP:
            return
        if not P.verify_crc(raw):
            log.warning("Failed CRC on a blockrequest received by the AP")
            return
        item = self.get_queue_item(br.src, br.ver)
        if item is None:
            await self.prepare_cancel_pending(br.src)
            log.info("blockrequest: couldn't find taginfo %s", P.mac_to_hex(br.src))
            return
        data = self.load_item_data(item)
        if data is None:
            log.info("No current file. %s Canceling request", item.filename)
            await self.prepare_cancel_pending(br.src)
            return
        total_blocks = max(1, (len(data) + P.BLOCK_DATA_SIZE - 1) // P.BLOCK_DATA_SIZE)
        block_id = min(br.block_id, total_blocks - 1)
        start = block_id * P.BLOCK_DATA_SIZE
        chunk = data[start:start + P.BLOCK_DATA_SIZE]
        checksum = None
        if self.serial is not None:
            checksum = await self.serial.send_block(chunk)
        self.events.ws_log(f"{P.mac_to_hex(br.src)} block request {item.filename} block {block_id}, "
                           f"len {len(chunk)} checksum {checksum if checksum is not None else 0}")
        self.ctx.hub.broadcast({"upload": {"src": P.mac_to_hex(br.src), "current": block_id + 1,
                                           "total": total_blocks}})

    async def process_xfer_complete(self, xfc: P.XferComplete, local: bool) -> None:
        if self.db.config.run_status == P.RUNSTATUS_STOP:
            return
        mac = xfc.src
        hexmac = P.mac_to_hex(mac)
        self.events.ws_log(f"{hexmac} reports xfer complete")
        now = int(time.time())
        dst_path = self._current_path(mac, ".raw")
        md5 = b"\x00" * 16
        was_deepsleep = False
        item = self.get_queue_item(mac)
        if item is not None:
            avail = item.pending.avail
            if avail.data_type == P.DATATYPE_COMMAND_DATA and avail.data_type_argument == P.CMD_DO_DEEPSLEEP:
                was_deepsleep = True
            if item.filename and self.fs.exists(item.filename):
                if self.fs.exists(dst_path):
                    self.fs.remove(dst_path)
                if self.db.config.preview and avail.data_type not in (P.DATATYPE_FW_UPDATE, P.DATATYPE_NOUPDATE):
                    self.fs.rename(item.filename, dst_path)
                elif avail.data_type != P.DATATYPE_FW_UPDATE:
                    self.fs.remove(item.filename)
            md5 = data_ver_to_md5(avail.data_ver)
            self.dequeue_item(mac)

        record = self.db.find(mac)
        if record is not None:
            self.db.clear_pending(record)
            record.md5 = md5
            record.update_count += 1
            record.update_last = now
            record.pending_count = self.count_queue_item(mac)
            record.wakeup_reason = 0
            if was_deepsleep:
                record.expected_next_checkin = P.NO_UPDATE_SENTINEL
            if record.content_mode == 12 and not local:
                self.fs.remove(dst_path)
            if record.content_mode in (5, 17, 18):
                self.db.pop_taginfo(mac)
                record = self.db.find(mac)
                if record is not None:
                    record.nextupdate = now
        if local:
            await self.check_queue(mac)
        self._ws_taginfo(mac, P.SYNC_TAGSTATUS)
        if local and self.udp is not None:
            self.udp.net_process_xfer_complete(xfc)

    async def process_xfer_timeout(self, xfc: P.XferComplete, local: bool) -> None:
        if self.db.config.run_status == P.RUNSTATUS_STOP:
            return
        mac = xfc.src
        self.events.ws_err(f"< {P.mac_to_hex(mac)} xfer timeout")
        record = self.db.find(mac)
        if record is not None:
            record.pending_idle = 60
            self.db.clear_pending(record)
        while self.dequeue_item(mac):
            pass
        await self.check_queue(mac)
        self._ws_taginfo(mac, P.SYNC_TAGSTATUS)
        if local and self.udp is not None:
            self.udp.net_process_xfer_timeout(xfc)

    async def process_data_req(self, eadr: P.EspAvailDataReq, local: bool, remote_ip: str | None) -> None:
        cfg = self.db.config
        if cfg.run_status == P.RUNSTATUS_STOP:
            return
        mac = eadr.src
        adr = eadr.adr
        hexmac = P.mac_to_hex(mac)
        info = self.ctx.ap_info
        record = self.db.find(mac)
        if record is None:
            if cfg.lock == 1 or (cfg.lock == 2 and adr.wakeup_reason != P.WAKEUP_REASON_FIRSTBOOT):
                return
            subghz_ok = info.has_subghz and adr.current_channel > 0 and adr.current_channel == info.subghz_channel
            if not subghz_ok and local and adr.current_channel > 0 and adr.current_channel != info.channel:
                log.info("Tag %s reports illegal channel %d", hexmac, adr.current_channel)
                return
            record = TagRecord(mac=bytes(mac))
            self.db.records.append(record)
        now = int(time.time())

        if not local:
            if not record.is_external:
                self.events.ws_log("moved AP from local to external " + hexmac)
                record.is_external = True
            record.ap_ip = remote_ip or "0.0.0.0"
        else:
            if record.is_external:
                self.events.ws_log("moved AP from external to local " + hexmac)
                record.is_external = False
            record.ap_ip = "0.0.0.0"

        if record.pending_idle == 0 or self.count_queue_item(mac) > 0:
            if record.expected_next_checkin < now + 60 or record.expected_next_checkin == P.NO_UPDATE_SENTINEL:
                record.expected_next_checkin = now + 60
        else:
            record.expected_next_checkin = now + record.pending_idle
        record.pending_idle = 0
        record.lastseen = now

        if adr.rssi != 0:
            if adr.wakeup_reason >= 0xE0:
                if record.pending_count == 0:
                    record.nextupdate = 0
                    record.md5 = b"\x00" * 16
                if local:
                    reason = {
                        P.WAKEUP_REASON_FIRSTBOOT: "Booting",
                        P.WAKEUP_REASON_NETWORK_SCAN: "Network scan",
                        P.WAKEUP_REASON_WDT_RESET: "Watchdog reset",
                        P.WAKEUP_REASON_FAILED_OTA_FW: "Firmware update rejected",
                    }.get(adr.wakeup_reason, "")
                    self.events.log_line(f"{hexmac} {reason}")
            record.lqi = adr.lqi
            record.rssi = adr.rssi
            record.temperature = adr.temperature
            record.battery_mv = adr.battery_mv
            record.hw_type = adr.hw_type
            record.wakeup_reason = adr.wakeup_reason
            record.capabilities = adr.capabilities
            record.current_channel = adr.current_channel
            record.tag_software_version = adr.tag_software_version

        if local:
            log.info("<ADR %s", hexmac)
            await self.check_queue(mac)
            # A delivered deep-sleep command never produces an xfer complete.
            if not (mac[7] == 0x13 and mac[6] == 0x37):
                sent = self.get_queue_item(mac)
                if sent is not None and sent.pending.avail.data_type == P.DATATYPE_COMMAND_DATA and \
                        sent.pending.avail.data_type_argument == P.CMD_DO_DEEPSLEEP:
                    self.dequeue_item(mac, sent.data_ver)
                    record.pending_count = self.count_queue_item(mac)
                    record.expected_next_checkin = P.NO_UPDATE_SENTINEL
                    if self.serial is not None:
                        await self.serial.send_cancel_pending(P.PendingData(target_mac=bytes(mac)))
            self._ws_taginfo(mac, P.SYNC_TAGSTATUS)
            if self.udp is not None:
                self.udp.net_process_data_req(eadr)
        else:
            self._ws_taginfo(mac, P.SYNC_NOSYNC)

    def process_tag_return_data(self, raw: bytes) -> None:
        """``processTagReturnData``: custom data a tag sent back to the AP."""
        if len(raw) < 20 or not P.verify_crc(raw):
            return
        src = raw[1:9]
        total_len = raw[9]
        # tagReturnData: checksum, partId, dataVer u64, dataType, data[]
        _rd_checksum, _part_id, data_ver, data_type = struct.unpack_from("<BBQB", raw, 10)
        payload_len = max(0, total_len - 11)
        payload = raw[21:21 + payload_len]
        hexmac = P.mac_to_hex(src)
        self.events.ws_log(f"<TRD {hexmac}")
        self.events.ws_log(f"TRD Data: len={payload_len}, type={data_type}, ver=0x{data_ver & 0xFFFFFFFF:08X}")
        self._parse_tag_data(hexmac, data_type, payload)

    def _parse_tag_data(self, hexmac: str, parser_id: int, payload: bytes) -> None:
        """Apply ``parsers.json`` (``tagdata.cpp``) and store the fields as variables."""
        parsers = self._load_parsers()
        parser = parsers.get(parser_id)
        if parser is None:
            self.events.ws_err(f"Error: No parser with id {parser_id} found({len(parsers)})")
            return
        offset = 0
        for field_def in parser.get("parser", []):
            name = str(field_def.get("name", ""))
            length = int(field_def.get("length", 0))
            ftype = int(field_def.get("type", 0))
            decimals = int(field_def.get("decimals", 0))
            mult = field_def.get("mult")
            if offset + length > len(payload):
                self.events.ws_err(f"Error: Not enough data for field {name}")
                return
            chunk = payload[offset:offset + length]
            offset += length
            value = ""
            if ftype == 0:  # INT
                value = f"{int.from_bytes(chunk, 'little', signed=True) * float(mult or 1):.{decimals}f}"
            elif ftype == 1:  # UINT
                value = f"{int.from_bytes(chunk, 'little') * float(mult or 1):.{decimals}f}"
            elif ftype == 2:  # FLOAT
                if length == 4:
                    value = f"{struct.unpack('<f', chunk)[0] * float(mult or 1):.{decimals}f}"
                elif length == 8:
                    value = f"{struct.unpack('<d', chunk)[0] * float(mult or 1):.{decimals}f}"
                else:
                    self.events.ws_err("Error: Float can only be 4 or 8 bytes long")
            elif ftype == 3:  # STRING
                value = chunk.split(b"\x00", 1)[0].decode("utf-8", "replace")
            else:
                self.events.ws_err(f"Error: Type {ftype} not implemented")
            if not value:
                self.events.ws_err(f"Error: Empty value for field {name}")
                continue
            self.db.vars.set(f"{hexmac}.{name}", value)

    def _load_parsers(self) -> dict[int, dict]:
        cached = getattr(self, "_parsers", None)
        if cached is not None:
            return cached
        result: dict[int, dict] = {}
        data = self.fs.read_bytes("/parsers.json")
        if data:
            try:
                for entry in json.loads(data.decode("utf-8")):
                    if isinstance(entry, dict) and "id" in entry and "name" in entry:
                        result[int(entry["id"])] = entry
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                log.warning("parsers.json invalid: %s", exc)
        self._parsers = result
        return result

    # ------------------------------------------------------------------
    # misc API used by web / content manager
    # ------------------------------------------------------------------
    def refresh_all_pending(self) -> None:
        for record in self.db.records:
            if record.pending_count > 0 and record.version == 0:
                self.db.clear_pending(record)
                record.nextupdate = 0
                self._ws_taginfo(record.mac, P.SYNC_TAGSTATUS)

    refresh_all_pending_sync = refresh_all_pending

    async def update_content(self, mac: bytes) -> None:
        record = self.db.find(mac)
        if record is not None:
            self.db.clear_pending(record)
            record.md5 = b"\x00" * 16
            record.nextupdate = 0
            self._ws_taginfo(mac, P.SYNC_TAGSTATUS)

    async def set_ap_channel(self) -> None:
        """``setAPchannel``: push the configured channel to the radio."""
        cfg = self.db.config
        serial = self.serial
        if serial is None:
            return
        send = False
        if cfg.channel == 0:
            if self.udp is not None:
                self.udp.get_ap_list()
        elif serial.cur_channel.channel != cfg.channel:
            serial.cur_channel.channel = cfg.channel
            send = True
        if serial.cur_channel.subghz_channel != cfg.subghzchannel:
            serial.cur_channel.subghz_channel = cfg.subghzchannel
            self.ctx.ap_info.subghz_channel = cfg.subghzchannel
            send = True
        if send:
            scp = P.SetChannelPower(channel=0 if cfg.channel == 0 else serial.cur_channel.channel,
                                    power=serial.cur_channel.power,
                                    subghz_channel=serial.cur_channel.subghz_channel)
            await serial.send_channel_power(scp)

    async def send_tag_command(self, mac: bytes, cmd: int, local: bool, payload: bytes | None = None) -> bool:
        avail = P.AvailDataInfo(data_type=P.DATATYPE_COMMAND_DATA, data_type_argument=cmd, next_check_in=0)
        if payload is not None:
            buf = bytes(payload) + b"\x00" * 12
            avail.data_ver = int.from_bytes(buf[:8], "little")
            avail.data_size = int.from_bytes(buf[8:12], "little")
        pending = P.PendingData(avail=avail, attempts_left=P.MAX_XFER_ATTEMPTS, target_mac=bytes(mac))
        log.info(">Tag CMD %s", P.mac_to_hex(mac))
        record = self.db.find(mac)
        if record is not None:
            record.pending_count += 1
            self._ws_taginfo(mac, P.SYNC_TAGSTATUS)
        if local:
            return await self.queue_data_avail(pending, True)
        await self.queue_data_avail(pending, False)
        if self.udp is not None:
            self.udp.net_send_data_avail(pending)
        return True

    async def send_tag_mac(self, mac: bytes, new_mac: int, local: bool) -> bool:
        avail = P.AvailDataInfo(data_type=P.DATATYPE_COMMAND_DATA, data_type_argument=P.CMD_SET_MAC_ARG,
                                data_ver=new_mac, data_size=0, next_check_in=0)
        pending = P.PendingData(avail=avail, attempts_left=P.MAX_XFER_ATTEMPTS, target_mac=bytes(mac))
        record = self.db.find(mac)
        if record is not None:
            record.pending_count += 1
            self._ws_taginfo(mac, P.SYNC_TAGSTATUS)
        if local:
            return await self.queue_data_avail(pending, True)
        await self.queue_data_avail(pending, False)
        if self.udp is not None:
            self.udp.net_send_data_avail(pending)
        return True

    async def send_ap_segmented_data(self, mac: bytes, text: str, icons: int, inverted: bool, local: bool) -> bool:
        raw = (text.encode("ascii", "replace") + b"\x00" * 10)[:10]
        avail = P.AvailDataInfo(data_type=P.DATATYPE_UK_SEGMENTED, data_size=(icons << 16) & 0xFFFFFFFF,
                                data_ver=int.from_bytes(raw[:8], "little"), data_type_argument=int(inverted),
                                next_check_in=0)
        pending = P.PendingData(avail=avail, attempts_left=P.MAX_XFER_ATTEMPTS, target_mac=bytes(mac))
        if local:
            return await self.queue_data_avail(pending, True)
        await self.queue_data_avail(pending, False)
        if self.udp is not None:
            self.udp.net_send_data_avail(pending)
        return True

    def update_taginfo_item(self, taginfo: dict, remote_ip: str) -> None:
        """``updateTaginfoitem``: status of a tag owned by another AP arrived over UDP."""
        mac = taginfo["mac"]
        record = self.db.find(mac)
        if record is None:
            if self.db.config.lock:
                return
            record = TagRecord(mac=bytes(mac))
            self.db.records.append(record)
        before = record.to_json()
        sync_mode = taginfo["sync_mode"]
        if sync_mode == P.SYNC_USERCFG:
            record.alias = taginfo["alias"]
            record.nextupdate = taginfo["nextupdate"]
        elif sync_mode == P.SYNC_TAGSTATUS:
            record.lastseen = taginfo["lastseen"]
            record.nextupdate = taginfo["nextupdate"]
            record.pending_count = taginfo["pending_count"]
            record.expected_next_checkin = taginfo["expected_next_checkin"]
            record.hw_type = taginfo["hw_type"]
            record.wakeup_reason = taginfo["wakeup_reason"]
            record.capabilities = taginfo["capabilities"]
            record.pending_idle = taginfo["pending_idle"]
        remote_mode = taginfo["content_mode"]
        if record.content_mode != 12 and remote_mode != 12 and remote_mode != 0:
            self.events.ws_log(f"Remote AP at {remote_ip} takes control over tag {P.mac_to_hex(mac)}")
            record.content_mode = 12
        if sync_mode == P.SYNC_DELETE:
            record.content_mode = 255
            self._ws_taginfo(mac, P.SYNC_NOSYNC)
            self.db.delete(mac)
        elif before != record.to_json():
            self._ws_taginfo(mac, P.SYNC_NOSYNC)


async def _http_get_bytes(url: str, timeout: float = 10.0) -> bytes | None:
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                return await response.read()
    except Exception as exc:
        log.warning("GET %s failed: %s", url, exc)
        return None
