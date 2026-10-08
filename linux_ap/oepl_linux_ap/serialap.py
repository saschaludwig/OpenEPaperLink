"""UART link to the ESP32-C6 radio (``serialap.cpp``).

Frames from the C6 start with a four character header (``ADR>``, ``RQB>``,
``XFC>`` ...) followed by a packed struct.  Commands to the C6 are a four
character header followed by a struct and are answered with ``ACK>``,
``NOK>`` or ``NOQ>`` within 200 ms.

The class owns the port, the parser state machine, the command lock and the
supervision loop that resets the C6 when it stops answering pings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from . import protocol as P
from .gpio import C6Control
from .state import ESP32_C6, AppContext, ap_type_name

if TYPE_CHECKING:  # pragma: no cover
    from .newproto import NewProto

log = logging.getLogger(__name__)

CMD_REPLY_TIMEOUT = 0.2
AP_ACTIVITY_MAX_INTERVAL = 30.0
BAUD_SLOW = 115200
BAUD_FAST = 2_000_000

# Parser states
_WAIT_HEADER = 0
_WAIT_FIXED = 1   # fixed size binary packet
_WAIT_HEX = 2     # fixed number of hex characters
_WAIT_TRD = 3     # tag return data, length byte at index 9
_WAIT_SCH = 4     # three decimal digits (sub GHz channel)


class SerialAP:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.settings = ctx.settings
        self.info = ctx.ap_info
        self.events = ctx.events
        self.gpio = C6Control(self.settings.gpio_chip, self.settings.gpio_reset_line, self.settings.gpio_boot_line)

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._transport = None
        self._rx_task: Optional[asyncio.Task] = None
        self._proc_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._tx_lock = asyncio.Lock()
        self._reply: Optional[asyncio.Future] = None
        self._rx_queue: asyncio.Queue = asyncio.Queue(maxsize=30)
        self._last_activity = time.monotonic()
        self._suspended = False
        self._online_event = asyncio.Event()
        # espSetChannelPower curChannel = {0, 11, 10}
        self.cur_channel = P.SetChannelPower(channel=11, power=10, subghz_channel=0)
        self.proto: Optional["NewProto"] = None

    # ------------------------------------------------------------------
    # Port handling
    # ------------------------------------------------------------------
    async def _open_port(self) -> bool:
        if self._writer is not None:
            return True
        try:
            import serial_asyncio

            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self.settings.serial_ap_port, baudrate=BAUD_SLOW, timeout=0)
            self._transport = self._writer.transport
            log.info("opened %s at %d baud", self.settings.serial_ap_port, BAUD_SLOW)
            return True
        except Exception as exc:
            log.error("cannot open %s: %s", self.settings.serial_ap_port, exc)
            self._reader = self._writer = self._transport = None
            return False

    def _set_baudrate(self, baud: int) -> None:
        if self._transport is None:
            return
        try:
            self._transport.serial.baudrate = baud
        except Exception as exc:  # pragma: no cover - hardware specific
            log.warning("baud rate change failed: %s", exc)

    async def close(self) -> None:
        for task in (self._rx_task, self._proc_task, self._monitor_task):
            if task is not None:
                task.cancel()
        self._rx_task = self._proc_task = self._monitor_task = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = self._writer = self._transport = None

    async def suspend_for_flashing(self) -> None:
        """Release the UART and GPIO lines so esptool can use them."""
        self._suspended = True
        self.set_state(False, P.AP_STATE_FLASHING)
        await self.close()
        await asyncio.sleep(0.5)

    async def resume_after_flashing(self) -> bool:
        self._suspended = False
        self.info.version = 0
        self.set_state(False, P.AP_STATE_OFFLINE)
        if not await self._open_port():
            return False
        self._start_tasks()
        await asyncio.sleep(1.0)
        ok = await self.bring_online()
        if ok and self.proto is not None:
            self.proto.refresh_all_pending()
        return ok

    def _start_tasks(self) -> None:
        loop = asyncio.get_running_loop()
        if self._rx_task is None or self._rx_task.done():
            self._rx_task = loop.create_task(self._rx_loop(), name="serial-rx")
        if self._proc_task is None or self._proc_task.done():
            self._proc_task = loop.create_task(self._process_loop(), name="serial-proc")
        if self.settings.serial_debug_port and (self._monitor_task is None or self._monitor_task.done()):
            self._monitor_task = loop.create_task(self._debug_monitor(), name="serial-monitor")

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def set_state(self, is_online: bool, state: int) -> None:
        self.info.is_online = is_online
        self.info.state = state
        if is_online:
            self._online_event.set()
        else:
            self._online_event.clear()
        from .web import build_sysinfo_payload

        try:
            self.events.ws_sysinfo(build_sysinfo_payload(self.ctx))
        except Exception:
            log.debug("sysinfo broadcast failed", exc_info=True)

    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------
    async def _wait_reply(self) -> bool:
        assert self._reply is not None
        try:
            value = await asyncio.wait_for(self._reply, CMD_REPLY_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        finally:
            self._reply = None
        self._last_activity = time.monotonic()
        if value == "ACK":
            if not self.info.is_online and self.info.state in (P.AP_STATE_ONLINE, P.AP_STATE_FAILED):
                self.set_state(True, P.AP_STATE_ONLINE)
            return True
        return False

    async def _command(self, header: bytes, payload: bytes = b"", attempts: int = 1,
                       retry_delay: float = 0.0, log_name: str = "") -> bool:
        if self._writer is None:
            return False
        for attempt in range(attempts):
            self._reply = asyncio.get_running_loop().create_future()
            try:
                self._writer.write(header + payload)
                await self._writer.drain()
            except Exception as exc:
                log.error("serial write failed: %s", exc)
                self._reply = None
                return False
            if await self._wait_reply():
                return True
            if log_name:
                log.debug("%s send failed in try %d", log_name, attempt)
            if retry_delay:
                await asyncio.sleep(retry_delay)
        return False

    async def send_ping(self) -> bool:
        if self.info.state == P.AP_STATE_NORADIO:
            return True
        if self.info.state == P.AP_STATE_FLASHING:
            return False
        async with self._tx_lock:
            return await self._command(b"RDY?", attempts=3, log_name="RDY?")

    async def send_get_info(self) -> bool:
        if self.info.state == P.AP_STATE_NORADIO:
            return True
        async with self._tx_lock:
            return await self._command(b"NFO?", attempts=5, log_name="NFO?")

    async def send_highspeed(self) -> bool:
        if self.info.state == P.AP_STATE_NORADIO:
            return True
        async with self._tx_lock:
            return await self._command(b"HSPD", attempts=5, log_name="HSPD")

    async def send_data_avail(self, pending: P.PendingData) -> bool:
        if self.info.state == P.AP_STATE_NORADIO:
            return True
        if not self.info.is_online:
            return False
        async with self._tx_lock:
            ok = await self._command(b"SDA>", pending.pack(), attempts=5, retry_delay=0.2, log_name="SDA")
        if not ok:
            log.warning("SDA failed to send")
        return ok

    async def send_cancel_pending(self, pending: P.PendingData) -> bool:
        if self.info.state == P.AP_STATE_NORADIO:
            return True
        if not self.info.is_online:
            return False
        async with self._tx_lock:
            ok = await self._command(b"CXD>", pending.pack(), attempts=5, log_name="CXD")
        if not ok:
            log.warning("CXD failed to send")
        return ok

    async def send_channel_power(self, scp: P.SetChannelPower) -> bool:
        if self.info.state == P.AP_STATE_NORADIO:
            return True
        if self.info.state not in (P.AP_STATE_ONLINE, P.AP_STATE_COMING_ONLINE):
            return False
        async with self._tx_lock:
            ok = await self._command(b"SCP>", scp.pack(), attempts=5, log_name="SCP")
        if ok:
            self.info.channel = scp.channel
            self.info.power = scp.power
        else:
            log.warning("SCP failed to send")
        return ok

    async def send_block(self, data: bytes) -> int | None:
        """``sendBlock``: push up to 4 kB to the C6, return the checksum."""
        if self.info.state == P.AP_STATE_NORADIO:
            return 0
        if not self.info.is_online or self._writer is None:
            return None
        started = time.monotonic()
        async with self._tx_lock:
            if not await self._command(b">D>", attempts=1, log_name=">D>"):
                log.warning("Failed sending block")
                return None
            frame = P.build_block_transfer(data)
            try:
                self._writer.write(frame)
                await self._writer.drain()
            except Exception as exc:
                log.error("block write failed: %s", exc)
                return None
            if self.info.type != ESP32_C6:
                await asyncio.sleep(0.01)
        log.debug("Sendblock complete, %d ms", int((time.monotonic() - started) * 1000))
        return sum(data) & 0xFFFF

    # ------------------------------------------------------------------
    # Bring-up and supervision
    # ------------------------------------------------------------------
    def hard_reset(self) -> None:
        log.info("Resetting C6")
        self.gpio.hard_reset()

    async def bring_online(self, new_state: int = P.AP_STATE_ONLINE) -> bool:
        """``bringAPOnline``: ping, reset if needed, set channel, get info, go fast."""
        if self.info.state == P.AP_STATE_NORADIO:
            return True
        if self.info.state == P.AP_STATE_FLASHING:
            return False
        if not await self._open_port():
            return False
        self._start_tasks()
        self.set_state(False, P.AP_STATE_OFFLINE)
        self._set_baudrate(BAUD_SLOW)
        ready = await self.send_ping()
        if not ready:
            if self.info.state == P.AP_STATE_FLASHING:
                return False
            await asyncio.get_running_loop().run_in_executor(None, self.hard_reset)
            await asyncio.sleep(1.0)
            deadline = time.monotonic() + 10.0
            while not ready and time.monotonic() < deadline and self.info.state != P.AP_STATE_FLASHING:
                ready = await self.send_ping()
                await asyncio.sleep(0.3)
        if not ready:
            return False
        self.set_state(False, P.AP_STATE_COMING_ONLINE)
        await self.send_channel_power(self.cur_channel)
        await asyncio.sleep(0.2)
        if not await self.send_get_info():
            self.set_state(False, P.AP_STATE_OFFLINE)
            return False
        if self.info.type == ESP32_C6 and self.settings.serial_ap_high_speed:
            if await self.send_highspeed():
                await asyncio.sleep(0.01)
                self._set_baudrate(BAUD_FAST)
                log.info("switched to %d baud", BAUD_FAST)
        await asyncio.sleep(0.2)
        self.set_state(new_state == P.AP_STATE_ONLINE, new_state)
        log.info("AP online: type %02X (%s) ch %d power %02X mac %s version %04X",
                 self.info.type, ap_type_name(self.info.type), self.info.channel, self.info.power,
                 P.mac_to_hex(self.info.mac), self.info.version)
        return True

    async def run(self) -> None:
        """``APTask``: initial bring-up followed by the keep-alive loop."""
        if not await self._open_port():
            self.events.ws_err(f"Cannot open {self.settings.serial_ap_port}; running without radio")
            self.set_state(False, P.AP_STATE_FAILED)
            # retry periodically – the device may appear later
            while True:
                await asyncio.sleep(30)
                if await self._open_port():
                    break
        self._start_tasks()
        if await self.bring_online():
            if self.proto is not None:
                self.proto.refresh_all_pending()
        else:
            self.events.ws_err("I wasn't able to connect to the AP radio. Did you flash it?")
            if self.info.state != P.AP_STATE_FLASHING:
                self.set_state(False, P.AP_STATE_FAILED)

        attempts = 0
        while True:
            await asyncio.sleep(1.0)
            if self._suspended:
                continue
            if self.info.state in (P.AP_STATE_ONLINE, P.AP_STATE_FAILED) and \
                    time.monotonic() - self._last_activity > AP_ACTIVITY_MAX_INTERVAL:
                if await self.send_ping():
                    if not self.info.is_online:
                        self.set_state(True, P.AP_STATE_ONLINE)
                    attempts = 0
                else:
                    attempts += 1
                if attempts > 5 and self.info.state != P.AP_STATE_FLASHING:
                    self.set_state(False, P.AP_STATE_WAIT_RESET)
                    if await self.bring_online():
                        self.set_state(True, P.AP_STATE_ONLINE)
                        attempts = 0
                        if self.proto is not None:
                            self.proto.refresh_all_pending()
                    else:
                        self.set_state(False, P.AP_STATE_FAILED)
                        self._last_activity = time.monotonic()

    # ------------------------------------------------------------------
    # Receive path
    # ------------------------------------------------------------------
    async def _rx_loop(self) -> None:
        state = _WAIT_HEADER
        header = bytearray(4)
        packet = bytearray()
        expected = 0
        kind = ""
        hex_target = ""
        hex_count = 0
        reader = self._reader
        assert reader is not None
        while True:
            try:
                chunk = await reader.read(512)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("serial read failed: %s", exc)
                await asyncio.sleep(1.0)
                continue
            if not chunk:
                await asyncio.sleep(0.01)
                continue
            for byte in chunk:
                if state == _WAIT_HEADER:
                    header[0:3] = header[1:4]
                    header[3] = byte
                    head = bytes(header)
                    if head == b"ACK>":
                        self._set_reply("ACK")
                    elif head == b"NOK>":
                        self._set_reply("NOK")
                    elif head == b"NOQ>":
                        self._set_reply("NOQ")
                    elif head == b"VER>":
                        state, hex_target, hex_count, packet = _WAIT_HEX, "version", 4, bytearray()
                    elif head == b"MAC>":
                        state, hex_target, hex_count, packet = _WAIT_HEX, "mac", 16, bytearray()
                    elif head == b"ZCH>":
                        state, hex_target, hex_count, packet = _WAIT_HEX, "channel", 2, bytearray()
                    elif head == b"SCH>":
                        state, packet = _WAIT_SCH, bytearray()
                    elif head == b"ZPW>":
                        state, hex_target, hex_count, packet = _WAIT_HEX, "power", 2, bytearray()
                    elif head == b"PEN>":
                        state, hex_target, hex_count, packet = _WAIT_HEX, "pending", 2, bytearray()
                    elif head == b"NOP>":
                        state, hex_target, hex_count, packet = _WAIT_HEX, "nop", 2, bytearray()
                    elif head == b"TYP>":
                        state, hex_target, hex_count, packet = _WAIT_HEX, "type", 2, bytearray()
                    elif head == b"RES>":
                        self._enqueue("RSET", b"")
                    elif head == b"RQB>":
                        state, kind, expected, packet = _WAIT_FIXED, "RQB", 18, bytearray()
                        self._last_activity = time.monotonic()
                    elif head == b"ADR>":
                        state, kind, expected, packet = _WAIT_FIXED, "ADR", P.ESP_AVAIL_DATA_REQ_SIZE, bytearray()
                        self._last_activity = time.monotonic()
                    elif head == b"XFC>":
                        state, kind, expected, packet = _WAIT_FIXED, "XFC", 9, bytearray()
                    elif head == b"XTO>":
                        state, kind, expected, packet = _WAIT_FIXED, "XTO", 9, bytearray()
                    elif head == b"RDY>":
                        pass
                    elif head == b"TRD>":
                        state, packet = _WAIT_TRD, bytearray()
                        self._last_activity = time.monotonic()
                    if state != _WAIT_HEADER:
                        header[:] = b"\x00\x00\x00\x00"
                elif state == _WAIT_FIXED:
                    packet.append(byte)
                    if len(packet) == expected:
                        self._enqueue(kind, bytes(packet))
                        state = _WAIT_HEADER
                elif state == _WAIT_TRD:
                    packet.append(byte)
                    if len(packet) > 10 and len(packet) >= packet[9] + 10:
                        self._enqueue("TRD", bytes(packet))
                        state = _WAIT_HEADER
                elif state == _WAIT_HEX:
                    packet.append(byte)
                    if len(packet) == hex_count:
                        self._apply_hex(hex_target, bytes(packet))
                        state = _WAIT_HEADER
                elif state == _WAIT_SCH:
                    packet.append(byte)
                    if len(packet) == 3:
                        try:
                            channel = int(packet.decode("ascii", "replace").strip() or "255")
                        except ValueError:
                            channel = 255
                        if channel != 255:
                            self.info.has_subghz = True
                            self.info.subghz_channel = channel
                        else:
                            self.info.has_subghz = False
                            self.info.subghz_channel = 0
                        state = _WAIT_HEADER

    def _set_reply(self, value: str) -> None:
        if self._reply is not None and not self._reply.done():
            self._reply.set_result(value)

    def _apply_hex(self, target: str, raw: bytes) -> None:
        try:
            text = raw.decode("ascii")
            if target == "mac":
                self.info.mac = bytes.fromhex(text)
            else:
                value = int(text, 16)
                setattr(self.info, target, value)
        except ValueError:
            log.debug("bad hex field %s: %r", target, raw)

    def _enqueue(self, kind: str, data: bytes) -> None:
        try:
            self._rx_queue.put_nowait((kind, data))
        except asyncio.QueueFull:
            log.warning("rx queue full, dropping %s", kind)

    async def _process_loop(self) -> None:
        """``rxCmdProcessor``: handle queued frames once the AP is online."""
        while True:
            kind, data = await self._rx_queue.get()
            if not self.info.is_online:
                # The firmware only processes while online; wait instead of dropping.
                await self._online_event.wait()
            proto = self.proto
            if proto is None:
                continue
            try:
                if kind == "RQB":
                    await proto.process_block_request(P.BlockRequest.unpack(data), data)
                elif kind == "ADR":
                    if P.verify_crc(data):
                        await proto.process_data_req(P.EspAvailDataReq.unpack(data), True, None)
                    else:
                        log.warning("ADR with bad checksum")
                elif kind == "XFC":
                    await proto.process_xfer_complete(P.XferComplete.unpack(data), True)
                elif kind == "XTO":
                    await proto.process_xfer_timeout(P.XferComplete.unpack(data), True)
                elif kind == "RSET":
                    log.info("AP did reset, resending pending")
                    proto.refresh_all_pending()
                    await self.send_channel_power(self.cur_channel)
                elif kind == "TRD":
                    proto.process_tag_return_data(data)
            except Exception:
                log.exception("processing %s failed", kind)

    # ------------------------------------------------------------------
    # Debug UART monitor (rxSerialTask2)
    # ------------------------------------------------------------------
    async def _debug_monitor(self) -> None:
        """Watch the C6 console for the stuck-modem message and reset it."""
        try:
            import serial_asyncio

            reader, writer = await serial_asyncio.open_serial_connection(
                url=self.settings.serial_debug_port, baudrate=BAUD_SLOW)
        except Exception as exc:
            log.info("debug UART %s not available: %s", self.settings.serial_debug_port, exc)
            return
        holdoff = time.monotonic() - 30
        try:
            while True:
                try:
                    line = await reader.readline()
                except Exception:
                    await asyncio.sleep(1.0)
                    continue
                if not line:
                    await asyncio.sleep(0.05)
                    continue
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    log.debug("C6: %s", text)
                if text.startswith("receive buffer full, drop the current frame") and \
                        time.monotonic() - holdoff > 20:
                    holdoff = time.monotonic()
                    self.events.log_line("IEEE802.15.4 modem stuck case detected, resetting...")
                    self.ctx.db.config.run_status = P.RUNSTATUS_STOP
                    await asyncio.get_running_loop().run_in_executor(None, self.hard_reset)
                    await asyncio.sleep(1.0)
                    if await self.bring_online():
                        self.ctx.db.config.run_status = P.RUNSTATUS_RUN
                        self.events.log_line("IEEE802.15.4 modem reset ok")
                    else:
                        self.events.log_line("IEEE802.15.4 modem reset failed")
        finally:
            writer.close()
