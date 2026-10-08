"""Content types that send raw payloads instead of images (NFC, tag config, time)."""

from __future__ import annotations

import calendar
import time

from .. import protocol as P
from .util import cfg_int


def nfc_url_payload(url: str) -> bytes:
    """``prepareNFCReq``: NDEF URI record wrapped in a TLV."""
    raw = url.encode("utf-8")
    length = len(raw)
    data = bytearray()
    data.append(0x03)              # NDEF message TLV
    data.append((4 + length + 1) & 0xFF)
    data.append(0xD1)              # MB/ME/SR, well known type
    data.append(0x01)              # type length
    data.append((length + 1) & 0xFF)  # payload length
    data.append(0x55)              # 'U' URI record
    data.append(0x00)              # no URI prefix
    data += raw
    data.append(0xFE)              # terminator TLV
    return bytes(data)


def tag_config_payload(cfg: dict) -> bytes:
    """``prepareConfigFile``: struct tagsettings from the tag config form."""
    settings = P.TagSettings(
        settings_ver=1,
        enable_fast_boot=cfg_int(cfg, "fastboot"),
        enable_rf_wake=cfg_int(cfg, "rfwake"),
        enable_tag_roaming=cfg_int(cfg, "tagroaming"),
        enable_scan_for_ap_after_timeout=cfg_int(cfg, "tagscanontimeout"),
        enable_low_battery_symbol=cfg_int(cfg, "showlowbat"),
        enable_no_rf_symbol=cfg_int(cfg, "shownorf"),
        custom_mode=0,
        fast_boot_capabilities=0,
        minimum_check_in_time=1,
        fixed_channel=cfg_int(cfg, "fixedchannel"),
        bat_low_voltage=cfg_int(cfg, "lowvoltage"),
    )
    return settings.pack()


def _local_seconds(tm: time.struct_time) -> int:
    """``convert_tm_to_seconds``: wall clock time expressed as if it were UTC."""
    return calendar.timegm((tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, 0, 0, 0))


def time_raw_payload(now: int) -> bytes:
    """``prepareTIME_RAW``: local and unix time plus a design byte."""
    local_time = (_local_seconds(time.localtime(now)) + 20) & 0xFFFFFFFF
    unix_time = (now + 20) & 0xFFFFFFFF
    length = 1 + 4 + 4 + 1
    data = bytearray()
    data.append(length)
    data.append(0x01)
    data += local_time.to_bytes(4, "little")
    data += unix_time.to_bytes(4, "little")
    data.append(1)  # 1 = full date/time design
    return bytes(data)
