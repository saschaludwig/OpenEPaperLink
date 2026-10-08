"""Content type 19: JSON template (file, URL, or file with variables from a URL)."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ..image.encode import ImageParams, sprite_to_file
from .elements import ElementDrawer
from .util import format_http_date, http_get

if TYPE_CHECKING:  # pragma: no cover
    from ..tagdb import TagRecord
    from .manager import ContentManager

log = logging.getLogger(__name__)

_VAR_PATTERN = re.compile(r"\{\.([^}]*)\}")
_VAR_WITH_OP = re.compile(r"\{\.([^}]*)\}([*/+-])\{\.([^}]*)\}")


def _arduino_float_0(value: float) -> str:
    """``String(float, 0)``: rounded, no decimals."""
    return str(int(value + 0.5) if value >= 0 else -int(-value + 0.5))


def _to_float(text: str) -> float:
    """``String::toFloat``: leading number or 0."""
    match = re.match(r"\s*[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def extract_value_from_json(data: Any, path: str) -> str:
    """``extractValueFromJson``: resolve ``a.b.1.c``; floats get two decimals."""
    current = data
    for segment in [s for s in path.split(".") if s != ""]:
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list):
            try:
                index = int(segment)
            except ValueError:
                index = 0
            current = current[index] if 0 <= index < len(current) else None
        else:
            log.debug("Invalid JSON structure at path segment: %s", segment)
            return ""
    if current is None:
        return ""
    if isinstance(current, bool):
        return "true" if current else "false"
    if isinstance(current, float):
        # ArduinoJson reports whole floats as int, others get two decimals
        return str(int(current)) if current.is_integer() else f"{current:.2f}"
    if isinstance(current, (dict, list)):
        return json.dumps(current, separators=(",", ":"))
    return str(current)


def substitute_variables(text: str, variables: Any) -> str:
    """Replace ``{.path}`` and ``{.a}*{.b}`` style placeholders (``DataInterceptor``)."""

    def replace_op(match: re.Match) -> str:
        first = extract_value_from_json(variables, match.group(1))
        op = match.group(2)
        second = _to_float(extract_value_from_json(variables, match.group(3)))
        value = _to_float(first)
        if op == "*":
            return _arduino_float_0(value * second)
        if op == "/":
            return _arduino_float_0(value / second) if abs(second) > 0.0 else "0"
        if op == "+":
            return _arduino_float_0(value + second)
        return _arduino_float_0(value - second)

    text = _VAR_WITH_OP.sub(replace_op, text)
    return _VAR_PATTERN.sub(lambda m: extract_value_from_json(variables, m.group(1)), text)


def _parse_elements(text: str) -> list[dict] | None:
    """Parse a JSON array of elements, tolerating garbage before the ``[``."""
    start = text.find("[")
    if start == -1:
        return []
    try:
        doc = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        # mimic the streaming parser: salvage the elements before the error
        decoder = json.JSONDecoder()
        elements: list[dict] = []
        pos = start + 1
        while True:
            while pos < len(text) and text[pos] in " \r\n\t,":
                pos += 1
            if pos >= len(text) or text[pos] == "]":
                break
            try:
                obj, pos = decoder.raw_decode(text, pos)
            except json.JSONDecodeError:
                break
            if isinstance(obj, dict):
                elements.append(obj)
        log.warning("json error %s", exc)
        return elements if elements else None
    return [item for item in doc if isinstance(item, dict)] if isinstance(doc, list) else []


def draw_json_text(cm: "ContentManager", text: str, filename: str, record: "TagRecord", params: ImageParams) -> bool:
    """``drawJsonStream``: render the element list and write the image."""
    elements = _parse_elements(text)
    spr = cm.renderer.init_sprite(params.width, params.height, params)
    drawer = ElementDrawer(cm.renderer, cm.fs, spr, params)
    if elements is None:
        cm.ctx.events.ws_err("json error")
    else:
        drawer.draw_all(elements)
    return sprite_to_file(drawer.spr, cm.fs.path(filename), params)


def get_json_template_file(cm: "ContentManager", filename: str, jsonfile: str, record: "TagRecord",
                           params: ImageParams, variables: Any | None = None) -> bool:
    if not jsonfile.startswith("/"):
        jsonfile = "/" + jsonfile
    content = cm.fs.read_bytes(jsonfile)
    if content is None:
        return False
    text = content.decode("utf-8", errors="replace")
    if variables is not None:
        text = substitute_variables(text, variables)
    draw_json_text(cm, text, filename, record, params)
    return True


def get_json_template_url(cm: "ContentManager", filename: str, url: str, fetched: int, mac_hex: str,
                          record: "TagRecord", params: ImageParams) -> int:
    cm.ctx.events.log_line("http getJsonTemplateUrl " + url)
    response = http_get(url, 20.0, {"If-Modified-Since": format_http_date(fetched), "X-ESL-MAC": mac_hex})
    if response is None:
        cm.ctx.events.ws_err("http " + url + " failed")
        return 0
    if response.status_code == 200:
        draw_json_text(cm, response.text, filename, record, params)
    elif response.status_code != 304:
        cm.ctx.events.ws_err(f"http {url} status {response.status_code}")
    return response.status_code
