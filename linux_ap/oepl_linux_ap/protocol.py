"""Wire level definitions shared with the ESP32-C6 radio and the tags.

Everything in this module mirrors ``oepl-definitions.h``, ``oepl-proto.h``
and ``oepl-esp-ap-proto.h`` of the ESP firmware.  All structures are packed
little endian and carry an 8-bit additive checksum in their first byte.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Data types (oepl-definitions.h)
# --------------------------------------------------------------------------
DATATYPE_NOUPDATE = 0x00
DATATYPE_FW_UPDATE = 0x03
DATATYPE_IMG_RAW_1BPP = 0x20
DATATYPE_IMG_RAW_2BPP = 0x21
DATATYPE_IMG_RAW_3BPP = 0x22
DATATYPE_IMG_RAW_4BPP = 0x23
DATATYPE_IMG_ZLIB = 0x30
DATATYPE_IMG_G5 = 0x31
DATATYPE_UK_SEGMENTED = 0x51
DATATYPE_NFC_RAW_CONTENT = 0xA0
DATATYPE_NFC_URL_DIRECT = 0xA1
DATATYPE_TAG_CONFIG_DATA = 0xA8
DATATYPE_COMMAND_DATA = 0xAF
DATATYPE_CUSTOM_LUT_OTA = 0xC0  # TIME_RAW_DATA on newer firmwares

CMD_DO_REBOOT = 0
CMD_DO_SCAN = 1
CMD_DO_RESET_SETTINGS = 2
CMD_DO_DEEPSLEEP = 3
CMD_DO_LEDFLASH = 4
CMD_ERASE_EEPROM_IMAGES = 5
CMD_ENTER_SLIDESHOW_FAST = 6
CMD_ENTER_SLIDESHOW_MEDIUM = 7
CMD_ENTER_SLIDESHOW_SLOW = 8
CMD_ENTER_SLIDESHOW_GLACIAL = 9
CMD_ENTER_NORMAL_MODE = 10
CMD_ENTER_WAIT_RFWAKE = 11
CMD_GET_BATTERY_VOLTAGE = 12
CMD_SET_MAC_ARG = 0x23

# Wakeup reasons
WAKEUP_REASON_TIMED = 0
WAKEUP_REASON_BUTTON1 = 4
WAKEUP_REASON_BUTTON2 = 5
WAKEUP_REASON_BUTTON3 = 6
WAKEUP_REASON_GPIO = 2
WAKEUP_REASON_NFC = 3
WAKEUP_REASON_FAILED_OTA_FW = 0xE0
WAKEUP_REASON_FIRSTBOOT = 0xFC
WAKEUP_REASON_NETWORK_SCAN = 0xFD
WAKEUP_REASON_WDT_RESET = 0xFE

# LUT selection in imageDataTypeArgStruct
EPD_LUT_DEFAULT = 0
EPD_LUT_NO_REPEATS = 1
EPD_LUT_FAST_NO_REDS = 2
EPD_LUT_FAST = 3

# Capabilities
CAPABILITY_HAS_NFC = 0x08
CAPABILITY_NFC_WAKE = 0x10
CAPABILITY_HAS_WAKE_BUTTON = 0x20
CAPABILITY_HAS_EXT_POWER = 0x40
CAPABILITY_HAS_LED = 0x80

# Transport
BLOCK_DATA_SIZE = 4096
BLOCK_XFER_BUFFER_SIZE = BLOCK_DATA_SIZE + 4  # header {u16 size, u16 checksum}
MAX_XFER_ATTEMPTS = 20
NO_UPDATE_SENTINEL = 3216153600  # dataVer that means "nothing to send"

# UDP sync packet types (udp.cpp)
PKT_AVAIL_DATA_SHORTREQ = 0xE3
PKT_AVAIL_DATA_REQ = 0xE5
PKT_AVAIL_DATA_INFO = 0xE6
PKT_BLOCK_PARTIAL_REQUEST = 0xE7
PKT_BLOCK_REQUEST_ACK = 0xE9
PKT_BLOCK_REQUEST = 0xE4
PKT_BLOCK_PART = 0xE8
PKT_XFER_COMPLETE = 0xEA
PKT_XFER_COMPLETE_ACK = 0xEB
PKT_CANCEL_XFER = 0xEC
PKT_XFER_TIMEOUT = 0xED
PKT_PING = 0xED
PKT_PONG = 0xEE
PKT_APLIST_REQ = 0x80
PKT_APLIST_REPLY = 0x81
PKT_TAGINFO = 0x82

SYNC_NOSYNC = 0
SYNC_USERCFG = 1
SYNC_TAGSTATUS = 2
SYNC_DELETE = 3

# AP states (serialap.h)
AP_STATE_OFFLINE = 0
AP_STATE_ONLINE = 1
AP_STATE_FLASHING = 2
AP_STATE_WAIT_RESET = 3
AP_STATE_REQUIRED_POWER_CYCLE = 4
AP_STATE_FAILED = 5
AP_STATE_COMING_ONLINE = 6
AP_STATE_NORADIO = 7

# Run status (settings.h)
RUNSTATUS_STOP = 0
RUNSTATUS_PAUSE = 1
RUNSTATUS_RUN = 2
RUNSTATUS_INIT = 3

# Tag type names that have a dedicated meaning
SOLUM_SEG_UK = 0xF0  # segmented display
SOLUM_NODISPLAY = 0xF1


# --------------------------------------------------------------------------
# Checksum helper
# --------------------------------------------------------------------------
def add_crc(buffer: bytearray | bytes) -> bytes:
    """Return a copy of ``buffer`` with byte 0 set to the additive checksum."""
    data = bytearray(buffer)
    data[0] = sum(data[1:]) & 0xFF
    return bytes(data)


def verify_crc(buffer: bytes) -> bool:
    return (sum(buffer[1:]) & 0xFF) == buffer[0]


# --------------------------------------------------------------------------
# Struct layouts
# --------------------------------------------------------------------------
# AvailDataReq: 21 bytes
_AVAIL_DATA_REQ = struct.Struct("<BBbbHBBBHBB8s")
# espAvailDataReq: checksum + src[8] + AvailDataReq = 30 bytes
_ESP_AVAIL_DATA_REQ_PREFIX = struct.Struct("<B8s")
AVAIL_DATA_REQ_SIZE = _AVAIL_DATA_REQ.size
ESP_AVAIL_DATA_REQ_SIZE = _ESP_AVAIL_DATA_REQ_PREFIX.size + _AVAIL_DATA_REQ.size
# AvailDataInfo: 17 bytes
_AVAIL_DATA_INFO = struct.Struct("<BQIBBH")
# pendingData: AvailDataInfo + attemptsLeft u16 + targetMac[8] = 27 bytes
_PENDING_DATA = struct.Struct("<BQIBBHH8s")
# espBlockRequest: 18 bytes
_BLOCK_REQUEST = struct.Struct("<BQB8s")
# espXferComplete: 9 bytes
_XFER_COMPLETE = struct.Struct("<B8s")
# espSetChannelPower: 4 bytes
_SET_CHANNEL_POWER = struct.Struct("<BBBB")
# tagsettings: 14 bytes
_TAG_SETTINGS = struct.Struct("<BBBBBBBBBHHB")
# Block header inside the 4 kB transfer: u16 size, u16 checksum
_BLOCK_HEADER = struct.Struct("<HH")

PENDING_DATA_SIZE = _PENDING_DATA.size
BLOCK_REQUEST_SIZE = _BLOCK_REQUEST.size
XFER_COMPLETE_SIZE = _XFER_COMPLETE.size

assert _AVAIL_DATA_REQ.size == 21
assert ESP_AVAIL_DATA_REQ_SIZE == 30
assert _AVAIL_DATA_INFO.size == 17
assert _PENDING_DATA.size == 27
assert _BLOCK_REQUEST.size == 18
assert _XFER_COMPLETE.size == 9
assert _TAG_SETTINGS.size == 14


@dataclass
class AvailDataReq:
    """What a tag tells the AP when it checks in."""

    lqi: int = 0
    rssi: int = 0
    temperature: int = 0
    battery_mv: int = 0
    hw_type: int = 0
    wakeup_reason: int = 0
    capabilities: int = 0
    tag_software_version: int = 0
    current_channel: int = 0
    custom_mode: int = 0
    reserved: bytes = b"\x00" * 8

    @classmethod
    def unpack(cls, data: bytes) -> "AvailDataReq":
        (_crc, lqi, rssi, temp, batt, hw, wake, caps, ver, chan, custom, reserved) = _AVAIL_DATA_REQ.unpack(data[:21])
        return cls(lqi, rssi, temp, batt, hw, wake, caps, ver, chan, custom, reserved)

    def pack(self) -> bytes:
        raw = _AVAIL_DATA_REQ.pack(
            0, self.lqi, self.rssi, self.temperature, self.battery_mv, self.hw_type, self.wakeup_reason,
            self.capabilities, self.tag_software_version, self.current_channel, self.custom_mode, self.reserved
        )
        return add_crc(raw)


@dataclass
class EspAvailDataReq:
    src: bytes
    adr: AvailDataReq

    @classmethod
    def unpack(cls, data: bytes) -> "EspAvailDataReq":
        _crc, src = _ESP_AVAIL_DATA_REQ_PREFIX.unpack(data[:9])
        return cls(src, AvailDataReq.unpack(data[9:30]))

    def pack(self) -> bytes:
        raw = bytearray(_ESP_AVAIL_DATA_REQ_PREFIX.pack(0, self.src)) + self.adr.pack()
        return add_crc(raw)


@dataclass
class AvailDataInfo:
    data_ver: int = 0
    data_size: int = 0
    data_type: int = 0
    data_type_argument: int = 0
    next_check_in: int = 0

    @classmethod
    def unpack(cls, data: bytes) -> "AvailDataInfo":
        _crc, ver, size, dtype, arg, nci = _AVAIL_DATA_INFO.unpack(data[:17])
        return cls(ver, size, dtype, arg, nci)

    def pack(self) -> bytes:
        return add_crc(_AVAIL_DATA_INFO.pack(0, self.data_ver, self.data_size, self.data_type,
                                             self.data_type_argument, self.next_check_in))


@dataclass
class PendingData:
    """``struct pendingData``: AvailDataInfo plus routing information."""

    avail: AvailDataInfo = field(default_factory=AvailDataInfo)
    attempts_left: int = 0
    target_mac: bytes = b"\x00" * 8

    @classmethod
    def unpack(cls, data: bytes) -> "PendingData":
        _crc, ver, size, dtype, arg, nci, attempts, mac = _PENDING_DATA.unpack(data[:27])
        return cls(AvailDataInfo(ver, size, dtype, arg, nci), attempts, mac)

    def pack(self) -> bytes:
        return add_crc(_PENDING_DATA.pack(0, self.avail.data_ver, self.avail.data_size, self.avail.data_type,
                                          self.avail.data_type_argument, self.avail.next_check_in,
                                          self.attempts_left, self.target_mac))


@dataclass
class BlockRequest:
    ver: int
    block_id: int
    src: bytes

    @classmethod
    def unpack(cls, data: bytes) -> "BlockRequest":
        _crc, ver, block_id, src = _BLOCK_REQUEST.unpack(data[:18])
        return cls(ver, block_id, src)

    def pack(self) -> bytes:
        return add_crc(_BLOCK_REQUEST.pack(0, self.ver, self.block_id, self.src))


@dataclass
class XferComplete:
    src: bytes

    @classmethod
    def unpack(cls, data: bytes) -> "XferComplete":
        _crc, src = _XFER_COMPLETE.unpack(data[:9])
        return cls(src)

    def pack(self) -> bytes:
        return add_crc(_XFER_COMPLETE.pack(0, self.src))


@dataclass
class SetChannelPower:
    channel: int
    power: int
    subghz_channel: int = 0

    def pack(self) -> bytes:
        return add_crc(_SET_CHANNEL_POWER.pack(0, self.channel, self.power, self.subghz_channel))


@dataclass
class TagSettings:
    """``struct tagsettings`` sent with DATATYPE_TAG_CONFIG_DATA."""

    settings_ver: int = 1
    enable_fast_boot: int = 0
    enable_rf_wake: int = 0
    enable_tag_roaming: int = 0
    enable_scan_for_ap_after_timeout: int = 1
    enable_low_battery_symbol: int = 1
    enable_no_rf_symbol: int = 1
    fast_boot_capabilities: int = 0
    custom_mode: int = 0
    bat_low_voltage: int = 0
    minimum_check_in_time: int = 1
    fixed_channel: int = 0

    def pack(self) -> bytes:
        """14 byte struct without a checksum (``prepareConfigFile`` sends it as-is)."""
        return _TAG_SETTINGS.pack(
            self.settings_ver, self.enable_fast_boot, self.enable_rf_wake, self.enable_tag_roaming,
            self.enable_scan_for_ap_after_timeout, self.enable_low_battery_symbol, self.enable_no_rf_symbol,
            self.fast_boot_capabilities, self.custom_mode, self.bat_low_voltage & 0xFFFF,
            self.minimum_check_in_time & 0xFFFF, self.fixed_channel)


def pack_led_flash(mode: int, flash_duration: int, colors: tuple[int, int, int],
                   counts: tuple[int, int, int], speeds: tuple[int, int, int],
                   delays: tuple[int, int, int], repeats: int) -> bytes:
    """Pack ``struct ledFlash`` (12 bytes).

    Layout (LSB first within a byte)::

      byte0  mode:4, flashDuration:4
      byte1  color1 (RGB332)   byte2  flashCount1:4, flashSpeed1:4   byte3  delay1
      byte4  color2            byte5  flashCount2:4, flashSpeed2:4   byte6  delay2
      byte7  color3            byte8  flashCount3:4, flashSpeed3:4   byte9  delay3
      byte10 repeats           byte11 spare
    """
    data = bytearray(12)
    data[0] = (mode & 0xF) | ((flash_duration & 0xF) << 4)
    for index in range(3):
        base = 1 + index * 3
        data[base] = colors[index] & 0xFF
        data[base + 1] = (counts[index] & 0xF) | ((speeds[index] & 0xF) << 4)
        data[base + 2] = delays[index] & 0xFF
    data[10] = repeats & 0xFF
    return bytes(data)


def image_data_type_arg(lut: int, preload: bool = False, special_type: int = 0) -> int:
    """Pack ``imageDataTypeArgStruct`` (lut:2, preloadImage:1, specialType:5)."""
    return (lut & 0x3) | ((1 if preload else 0) << 2) | ((special_type & 0x1F) << 3)


def pack_block_header(size: int, checksum: int) -> bytes:
    return _BLOCK_HEADER.pack(size, checksum)


def build_block_transfer(data: bytes) -> bytes:
    """Return the 4 kB + header + trailer frame used after a ``>D>`` command.

    Header and payload are XORed with 0xAA, the remainder of the 4 kB block
    is padded with 0x55 and 32 bytes 0xF5 terminate the frame.
    """
    size = len(data)
    checksum = sum(data) & 0xFFFF
    header = pack_block_header(size, checksum)
    frame = bytearray(b ^ 0xAA for b in header + data)
    if size < BLOCK_DATA_SIZE:
        frame.extend(b"\x55" * (BLOCK_DATA_SIZE - size))
    frame.extend(b"\xF5" * 32)
    return bytes(frame)


# --------------------------------------------------------------------------
# MAC helpers (tag_db.cpp mac2hex / hex2mac)
# --------------------------------------------------------------------------
def mac_to_hex(mac: bytes) -> str:
    """Convert the 8 byte wire MAC into the 16 digit display form of the web UI.

    The firmware prints the bytes in reverse order (``mac[7]`` first).
    """
    mac = bytes(mac)
    if len(mac) != 8:
        raise ValueError("MAC must be 8 bytes")
    return "".join(f"{mac[i]:02X}" for i in range(7, -1, -1))


def hex_to_mac(text: str) -> bytes:
    """Parse the display MAC (12 or 16 hex digits) back into 8 wire bytes."""
    text = text.strip()
    if len(text) == 12:
        text = "0000" + text
    if len(text) != 16:
        raise ValueError(f"invalid MAC '{text}'")
    raw = bytes.fromhex(text)
    return bytes(reversed(raw))
