"""Small helpers shared by the content types (``util.h`` and friends)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import urllib.parse
from email.utils import format_datetime
from typing import Any

import requests

log = logging.getLogger(__name__)

USER_AGENT = "OpenEPaperLink-LinuxAP/0.1"


def is_empty_or_null(value: Any) -> bool:
    """``util::isEmptyOrNull``: treat ``None``, ``""`` and ``"null"`` as empty."""
    if value is None:
        return True
    text = str(value)
    return text == "" or text == "null"


def cfg_str(cfg: dict, key: str, default: str = "") -> str:
    value = cfg.get(key)
    if value is None:
        return default
    return str(value)


def cfg_int(cfg: dict, key: str, default: int = 0) -> int:
    """ArduinoJson style ``as<int>()``: tolerant conversion."""
    value = cfg.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip().split()[0])
    except (ValueError, IndexError):
        try:
            return int(float(str(value)))
        except ValueError:
            return default


def cfg_float(cfg: dict, key: str, default: float = 0.0) -> float:
    value = cfg.get(key)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except ValueError:
        try:
            return int(float(str(value)))
        except ValueError:
            return default


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def tpl(template: Any, index: int, default: Any = 0) -> Any:
    """Index into a template array like ArduinoJson does (missing → default)."""
    if isinstance(template, list) and 0 <= index < len(template):
        value = template[index]
        return default if value is None else value
    return default


def tpl_int(template: Any, index: int, default: int = 0) -> int:
    return as_int(tpl(template, index, default), default)


def tpl_str(template: Any, index: int, default: str = "") -> str:
    value = tpl(template, index, None)
    return default if value is None else as_str(value)


def now_epoch() -> int:
    return int(time.time())


def local_tm(epoch: int | None = None) -> time.struct_time:
    return time.localtime(now_epoch() if epoch is None else epoch)


def c_wday(tm: time.struct_time) -> int:
    """C ``tm_wday`` (Sunday = 0) from a Python struct_time (Monday = 0)."""
    return (tm.tm_wday + 1) % 7


def c_mon(tm: time.struct_time) -> int:
    """C ``tm_mon`` (0-11) from a Python struct_time (1-12)."""
    return tm.tm_mon - 1


def get_midnight_time() -> int:
    """``util::getMidnightTime``: epoch of the coming local midnight."""
    today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int((today + dt.timedelta(days=1)).timestamp())


def is_sleeping(sleeptime1: int, sleeptime2: int) -> bool:
    if sleeptime1 == sleeptime2:
        return False
    hour = time.localtime().tm_hour
    if sleeptime1 < sleeptime2:
        return sleeptime1 <= hour < sleeptime2
    return hour >= sleeptime1 or hour < sleeptime2


def format_http_date(epoch: int) -> str:
    """``formatHttpDate``: RFC 1123 date for ``If-Modified-Since``."""
    return format_datetime(dt.datetime.fromtimestamp(max(int(epoch), 0), dt.timezone.utc), usegmt=True)


def url_encode(text: str) -> str:
    return urllib.parse.quote(text, safe="-_.~")


def http_get_json(url: str, timeout: float = 5.0, headers: dict | None = None) -> Any | None:
    """``util::httpGetJson``: GET a JSON document; ``None`` on any failure."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    try:
        response = requests.get(url, timeout=timeout, headers=hdrs)
    except requests.RequestException as exc:
        log.warning("[httpGetJson] %s: %s", url, exc)
        return None
    if response.status_code != 200:
        log.warning("[httpGetJson] http %s code %d", url, response.status_code)
        return None
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("[httpGetJson] JSON: %s", exc)
        return None


def http_get(url: str, timeout: float = 5.0, headers: dict | None = None) -> requests.Response | None:
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    try:
        return requests.get(url, timeout=timeout, headers=hdrs, allow_redirects=True)
    except requests.RequestException as exc:
        log.warning("http %s: %s", url, exc)
        return None


def replace_html_entities(text: str) -> str:
    return (text.replace("&gt;", ">").replace("&lt;", "<").replace("&quot;", "\"")
            .replace("&apos;", "'").replace("&amp;", "&"))


def remove_html(text: str) -> str:
    out = []
    inside = False
    for char in text:
        if char == "<":
            inside = True
        elif char == ">":
            inside = False
        elif not inside:
            out.append(char)
    return "".join(out)


def c_round(value: float) -> int:
    """C ``round()``: halves away from zero."""
    if value >= 0:
        return int(value + 0.5)
    return -int(-value + 0.5)


def dtostrf(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def arduino_float_str(value: float) -> str:
    """``String(double)``: two decimals."""
    return f"{value:.2f}"
