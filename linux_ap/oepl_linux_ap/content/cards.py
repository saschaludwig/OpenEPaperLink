"""Content types that render a sprite from a template (``contentmanager.cpp``).

All functions here are synchronous and blocking (HTTP + rendering); the
:class:`~oepl_linux_ap.content.manager.ContentManager` runs them in a worker
thread.  Each one writes the encoded image to ``filename`` and returns
whatever the firmware counterpart returns.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from ..image.encode import ImageParams, sprite_to_file
from ..image.sprite import (
    TC_DATUM, TFT_BLACK, TFT_WHITE, TL_DATUM, TR_DATUM, Sprite,
)
from ..protocol import SOLUM_SEG_UK
from .drawing import Renderer
from .elements import ElementDrawer
from .util import (
    as_float, as_int, as_str, c_mon, c_round, c_wday, cfg_int, cfg_str, dtostrf, format_http_date, http_get, http_get_json,
    is_empty_or_null, local_tm, now_epoch, remove_html, replace_html_entities, tpl, tpl_int, tpl_str,
    url_encode,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..tagdb import TagRecord
    from .manager import ContentManager

log = logging.getLogger(__name__)

WEATHER_ICONS = ["\uf00d", "\uf00c", "\uf002", "\uf013", "\uf013", "\uf014", "", "", "\uf014", "", "",
                 "\uf01a", "", "\uf01a", "", "\uf01a", "\uf017", "\uf017", "", "", "",
                 "\uf019", "", "\uf019", "", "\uf019", "\uf015", "\uf015", "", "", "",
                 "\uf01b", "", "\uf01b", "", "\uf01b", "", "\uf076", "", "", "\uf01a",
                 "\uf01a", "\uf01a", "", "", "\uf064", "\uf064", "", "", "", "",
                 "", "", "", "", "\uf01e", "\uf01d", "", "", "\uf01e"]
NIGHT_ICONS = ["\uf02e", "\uf083", "\uf086"]
HIGHLIGHT_WEATHER_CODES = {55, 65, 75, 82, 86, 95, 96, 99}
WEATHER_FONT = "/fonts/weathericons.ttf"


def get_weather_icon(code: int, is_night: bool = False) -> str:
    if is_night and code <= 2:
        return NIGHT_ICONS[code]
    if 0 <= code < len(WEATHER_ICONS):
        return WEATHER_ICONS[code]
    return ""


def wind_speed_to_beaufort(wind_speed: float) -> int:
    speeds = (0.3, 1.5, 3.3, 5.5, 8, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7)
    beaufort = 0
    for index, speed in enumerate(speeds):
        if wind_speed >= speed:
            beaufort = index + 1
    return beaufort


def wind_direction_icon(degrees: int) -> str:
    directions = ("\uf044", "\uf043", "\uf048", "\uf087", "\uf058", "\uf057", "\uf04d", "\uf088")
    index = (int(degrees) + 22) // 45
    if index >= 8 or index < 0:
        index = 0
    return directions[index]


def utf8_from_codepoint(cp: int) -> str:
    return chr(cp)


def get_location(cm: "ContentManager", cfg: dict) -> None:
    """``getLocation``: geocode ``location`` into ``#lat``/``#lon``/``#tz``."""
    if not is_empty_or_null(cfg.get("#lat")) and not is_empty_or_null(cfg.get("#lon")):
        return
    cm.ctx.events.ws_log("get location")
    doc = http_get_json("https://geocoding-api.open-meteo.com/v1/search?name="
                        + url_encode(cfg_str(cfg, "location")) + "&count=1", 5.0)
    try:
        result = doc["results"][0]
    except (TypeError, KeyError, IndexError):
        return
    cfg["#lat"] = as_str(result.get("latitude"))
    cfg["#lon"] = as_str(result.get("longitude"))
    cfg["#tz"] = as_str(result.get("timezone"))


def _format_utc_to_local(text: str) -> str:
    """``formatUtcToLocal``: "HH:MM[:SS]" in UTC → local "HH:MM"."""
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return "-"
    if hour > 23 or minute > 59 or second > 59:
        return "-"
    import calendar
    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc).date()
    utc = dt.datetime(today.year, today.month, today.day, hour, minute, second, tzinfo=dt.timezone.utc)
    local = dt.datetime.fromtimestamp(calendar.timegm(utc.timetuple()))
    return local.strftime("%H:%M")


# --------------------------------------------------------------------------
# 1: date
# --------------------------------------------------------------------------
def draw_date(cm: "ContentManager", filename: str, cfg: dict, record: "TagRecord", params: ImageParams) -> None:
    r = cm.renderer
    lang = cm.ctx.language
    now = now_epoch()
    tm = local_tm(now)
    if record.hw_type == SOLUM_SEG_UK:
        params.segments = f"{tm.tm_mday:2d}{tm.tm_mon:2d}{lang.days[c_wday(tm)][:2]:<2.2}{tm.tm_year:04d}"
        params.symbols = 0x04
        return

    loc = r.get_template(1, record.hw_type)
    spr = r.init_sprite(params.width, params.height, params)
    date = loc.get("date")
    weekday = loc.get("weekday")
    month = loc.get("month")
    day = loc.get("day")

    if cfg.get("location"):
        lat = cfg_str(cfg, "#lat")
        lon = cfg_str(cfg, "#lon")
        doc = http_get_json(f"https://api.farmsense.net/v1/daylengths/?d={now}&lat={lat}&lon={lon}&tz=UTC", 5.0)
        if doc and isinstance(loc.get("sunrise"), list):
            try:
                sunrise = _format_utc_to_local(as_str(doc[0]["Sunrise"]))
                sunset = _format_utc_to_local(as_str(doc[0]["Sunset"]))
            except (KeyError, IndexError, TypeError):
                sunrise = sunset = "-"
            sunrise_icon = loc["sunrise"]
            sunset_icon = loc.get("sunset", [])
            r.draw_string(spr, "\uf046 ", tpl_int(sunrise_icon, 0), tpl_int(sunrise_icon, 1), WEATHER_FONT, TR_DATUM,
                          TFT_BLACK, tpl_int(sunrise_icon, 3))
            r.draw_string(spr, "\uf047 ", tpl_int(sunset_icon, 0), tpl_int(sunset_icon, 1), WEATHER_FONT, TR_DATUM,
                          TFT_BLACK, tpl_int(sunset_icon, 3))
            r.draw_string(spr, sunrise, tpl_int(sunrise_icon, 0), tpl_int(sunrise_icon, 1), tpl_str(sunrise_icon, 2),
                          TL_DATUM, TFT_BLACK, tpl_int(sunrise_icon, 3))
            r.draw_string(spr, sunset, tpl_int(sunset_icon, 0), tpl_int(sunset_icon, 1), tpl_str(sunset_icon, 2),
                          TL_DATUM, TFT_BLACK, tpl_int(sunset_icon, 3))

            moon = http_get_json(f"https://api.farmsense.net/v1/moonphases/?d={now}", 5.0)
            if moon and isinstance(loc.get("moonicon"), list):
                try:
                    moon_age = as_int(moon[0]["Index"])
                except (KeyError, IndexError, TypeError):
                    moon_age = 0
                moon_icon = loc["moonicon"]
                r.draw_string(spr, utf8_from_codepoint(0xF095 + moon_age), tpl_int(moon_icon, 0),
                              tpl_int(moon_icon, 1), WEATHER_FONT, TC_DATUM, TFT_BLACK, tpl_int(moon_icon, 2))
            date = loc.get("altdate")
            weekday = loc.get("altweekday")

    if isinstance(date, list):
        r.draw_string(spr, lang.days[c_wday(tm)], tpl_int(weekday, 0), tpl_int(weekday, 1), tpl_str(weekday, 2),
                      TC_DATUM, params.highlight_color, tpl_int(weekday, 3))
        r.draw_string(spr, f"{tm.tm_mday} {lang.months[c_mon(tm)]}", tpl_int(date, 0), tpl_int(date, 1),
                      tpl_str(date, 2), TC_DATUM, TFT_BLACK, tpl_int(date, 3))
    else:
        r.draw_string(spr, lang.days[c_wday(tm)], tpl_int(weekday, 0), tpl_int(weekday, 1), tpl_str(weekday, 2),
                      TC_DATUM, TFT_BLACK, tpl_int(weekday, 3))
        r.draw_string(spr, lang.months[c_mon(tm)], tpl_int(month, 0), tpl_int(month, 1), tpl_str(month, 2),
                      TC_DATUM, TFT_BLACK, tpl_int(month, 3))
        r.draw_string(spr, str(tm.tm_mday), tpl_int(day, 0), tpl_int(day, 1), tpl_str(day, 2), TC_DATUM,
                      params.highlight_color, tpl_int(day, 3))
    sprite_to_file(spr, cm.fs.path(filename), params)


# --------------------------------------------------------------------------
# 2/3: counters
# --------------------------------------------------------------------------
def draw_number(cm: "ContentManager", filename: str, count: int, threshold_red: int, record: "TagRecord",
                params: ImageParams) -> None:
    r = cm.renderer
    count_signed = count
    count = abs(count)
    if record.hw_type == SOLUM_SEG_UK:
        params.symbols = 0x00
        if count > 19999:
            params.segments = "over  flow"
            return
        if count > 9999:
            params.symbols = 0x02
            params.segments = f"{count - 10000:04d}"
        else:
            params.segments = f"{count:4d}"
        params.segments += "  hour" if record.content_mode == 3 else "  days"
        return

    loc = r.get_template(2, record.hw_type)
    spr = r.init_sprite(params.width, params.height, params)
    color = params.highlight_color if count_signed > threshold_red else TFT_BLACK
    fonts = loc.get("fonts", [])
    font = tpl_str(fonts, 0)
    size = tpl_int(fonts, 1)
    if count > 9:
        size = tpl_int(fonts, 2)
    if count > 99:
        size = tpl_int(fonts, 3)
    if count > 999:
        size = tpl_int(fonts, 4)
    if count > 9999:
        size = tpl_int(fonts, 5)
    if count > 99999:
        size = tpl_int(fonts, 6)
    xy = loc.get("xy", [])
    r.draw_string(spr, str(count), tpl_int(xy, 0), int(tpl_int(xy, 1) - size / 1.8), font, TC_DATUM, color, size)
    sprite_to_file(spr, cm.fs.path(filename), params)


# --------------------------------------------------------------------------
# 4/8: weather and forecast (open-meteo)
# --------------------------------------------------------------------------
def _draw_weather_content(doc: dict, loc: dict, spr: Sprite, cfg: dict, params: ImageParams, r: Renderer,
                          is_forecast: bool = False) -> None:
    current = doc.get("current_weather", {}) or {}
    temperature = as_float(current.get("temperature"))
    wind_speed = as_float(current.get("windspeed"))
    wind_direction = as_int(current.get("winddirection"))
    is_night = as_int(current.get("is_day")) == 0
    weather_code = as_int(current.get("weathercode"))
    if weather_code > 40:
        weather_code -= 40
    beaufort = wind_speed_to_beaufort(wind_speed)
    units = cfg_str(cfg, "units")
    wind_value = int(wind_speed) if units == "1" else beaufort

    if not is_forecast:
        location = loc.get("location", [])
        r.draw_string(spr, cfg_str(cfg, "location"), tpl_int(location, 0), tpl_int(location, 1), tpl_str(location, 2))
    wind = loc.get("currentwind" if is_forecast else "wind", [])
    r.draw_string(spr, str(wind_value), tpl_int(wind, 0), tpl_int(wind, 1), tpl_str(wind, 2), TR_DATUM,
                  params.highlight_color if beaufort > 4 else TFT_BLACK)

    temp = loc.get("temp", [])
    temperature_str = dtostrf(temperature, 1)
    if tpl_int(temp, 3) == 1:
        temperature_str += "°"
    r.draw_string(spr, temperature_str, tpl_int(temp, 0), tpl_int(temp, 1), tpl_str(temp, 2), TL_DATUM,
                  params.highlight_color if temperature < 0 else TFT_BLACK)

    icon_color = params.highlight_color if weather_code in HIGHLIGHT_WEATHER_CODES else TFT_BLACK
    icon = loc.get("currenticon" if is_forecast else "icon", [])
    r.draw_string(spr, get_weather_icon(weather_code, is_night), tpl_int(icon, 0), tpl_int(icon, 1), WEATHER_FONT,
                  tpl_int(icon, 3), icon_color, tpl_int(icon, 2))
    direction = loc.get("dir", [])
    r.draw_string(spr, wind_direction_icon(wind_direction), tpl_int(direction, 0), tpl_int(direction, 1),
                  WEATHER_FONT, TC_DATUM, TFT_BLACK, tpl_int(direction, 2))
    if weather_code > 10:
        umbrella = loc.get("umbrella", [])
        r.draw_string(spr, "\uf084", tpl_int(umbrella, 0), tpl_int(umbrella, 1), WEATHER_FONT, TC_DATUM,
                      params.highlight_color, tpl_int(umbrella, 2))


def _units_query(cfg: dict) -> str:
    if cfg_str(cfg, "units") == "1":
        return "&temperature_unit=fahrenheit&windspeed_unit=mph&precipitation_unit=inch"
    return ""


def draw_weather(cm: "ContentManager", filename: str, cfg: dict, record: "TagRecord", params: ImageParams) -> None:
    r = cm.renderer
    cm.ctx.events.ws_log("get weather")
    get_location(cm, cfg)
    lat, lon, tz = cfg_str(cfg, "#lat"), cfg_str(cfg, "#lon"), cfg_str(cfg, "#tz")
    doc = http_get_json(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                        f"&current_weather=true&windspeed_unit=ms&timezone={tz}{_units_query(cfg)}", 5.0)
    if not doc:
        return
    loc = r.get_template(4, record.hw_type)
    spr = r.init_sprite(params.width, params.height, params)
    _draw_weather_content(doc, loc, spr, cfg, params, r)
    sprite_to_file(spr, cm.fs.path(filename), params)


def draw_forecast(cm: "ContentManager", filename: str, cfg: dict, record: "TagRecord", params: ImageParams) -> None:
    r = cm.renderer
    lang = cm.ctx.language
    cm.ctx.events.ws_log("get weather")
    get_location(cm, cfg)
    lat, lon, tz = cfg_str(cfg, "#lat"), cfg_str(cfg, "#lon"), cfg_str(cfg, "#tz")
    doc = http_get_json(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                        "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                        "windspeed_10m_max,winddirection_10m_dominant&current_weather=true&windspeed_unit=ms"
                        f"&timeformat=unixtime&timezone={tz}{_units_query(cfg)}", 5.0)
    if not doc:
        return
    loc = r.get_template(8, record.hw_type)
    spr = r.init_sprite(params.width, params.height, params)
    if loc.get("temp"):
        _draw_weather_content(doc, loc, spr, cfg, params, r, True)

    location = loc.get("location", [])
    r.draw_string(spr, cfg_str(cfg, "location"), tpl_int(location, 0), tpl_int(location, 1), tpl_str(location, 2),
                  TL_DATUM, TFT_BLACK)
    daily = doc.get("daily", {}) or {}
    column = loc.get("column", [])
    column1 = tpl_int(column, 1)
    day = loc.get("day", [])
    icon = loc.get("icon", [])
    wind_tpl = loc.get("wind", [])
    rain_tpl = loc.get("rain")
    line = loc.get("line", [])
    utc_offset = as_int(doc.get("utc_offset_seconds"))
    units = cfg_str(cfg, "units")

    def daily_value(key: str, index: int, default=0):
        values = daily.get(key)
        if isinstance(values, list) and index < len(values) and values[index] is not None:
            return values[index]
        return default

    for dag in range(tpl_int(column, 0)):
        weather_day = as_int(daily_value("time", dag)) + utc_offset
        datum = time.localtime(weather_day)
        r.draw_string(spr, lang.days_short[c_wday(datum)], dag * column1 + tpl_int(day, 0), tpl_int(day, 1),
                      tpl_str(day, 2), TC_DATUM, TFT_BLACK)
        weather_code = as_int(daily_value("weathercode", dag))
        if weather_code > 40:
            weather_code -= 40
        icon_color = params.highlight_color if weather_code in HIGHLIGHT_WEATHER_CODES else TFT_BLACK
        r.draw_string(spr, get_weather_icon(weather_code), tpl_int(icon, 0) + dag * column1, tpl_int(icon, 1),
                      WEATHER_FONT, TC_DATUM, icon_color, tpl_int(icon, 2))
        r.draw_string(spr, wind_direction_icon(as_int(daily_value("winddirection_10m_dominant", dag))),
                      tpl_int(wind_tpl, 0) + dag * column1, tpl_int(wind_tpl, 1), WEATHER_FONT, TC_DATUM, TFT_BLACK,
                      tpl_int(icon, 2))
        tmin = c_round(as_float(daily_value("temperature_2m_min", dag)))
        tmax = c_round(as_float(daily_value("temperature_2m_max", dag)))
        wind_max = as_float(daily_value("windspeed_10m_max", dag))
        beaufort = wind_speed_to_beaufort(wind_max)
        wind = int(wind_max) if units == "1" else beaufort

        if rain_tpl:
            if units == "0":
                rain = c_round(as_float(daily_value("precipitation_sum", dag)))
                if rain > 0:
                    r.draw_string(spr, f"{rain}mm", dag * column1 + tpl_int(rain_tpl, 0), tpl_int(rain_tpl, 1),
                                  tpl_str(day, 2), TC_DATUM, params.highlight_color if rain > 10 else TFT_BLACK)
            else:
                f_rain = round(as_float(daily_value("precipitation_sum", dag)) * 100.0) / 100.0
                if f_rain > 0.0:
                    r.draw_string(spr, f"{f_rain:.2f}in", dag * column1 + tpl_int(rain_tpl, 0), tpl_int(rain_tpl, 1),
                                  tpl_str(day, 2), TC_DATUM, params.highlight_color if f_rain > 0.5 else TFT_BLACK)

        r.draw_string(spr, f"{tmin} ", dag * column1 + tpl_int(day, 0), tpl_int(day, 4), tpl_str(day, 2), TR_DATUM,
                      params.highlight_color if tmin < 0 else TFT_BLACK)
        r.draw_string(spr, f" {tmax}", dag * column1 + tpl_int(day, 0), tpl_int(day, 4), tpl_str(day, 2), TL_DATUM,
                      params.highlight_color if tmax < 0 else TFT_BLACK)
        r.draw_string(spr, f" {wind}", dag * column1 + column1 // 2, tpl_int(day, 3), tpl_str(day, 2), TL_DATUM,
                      params.highlight_color if beaufort > 5 else TFT_BLACK)
        if dag > 0:
            for i in range(tpl_int(line, 0), tpl_int(line, 1), 3):
                spr.draw_pixel(dag * column1, i, TFT_BLACK)
    sprite_to_file(spr, cm.fs.path(filename), params)


# --------------------------------------------------------------------------
# 7: image from URL
# --------------------------------------------------------------------------
def get_img_url(cm: "ContentManager", filename: str, url: str, fetched: int, params: ImageParams, mac_hex: str) -> int:
    """``getImgURL``: download with If-Modified-Since and convert; returns the HTTP status."""
    from ..image.encode import jpg_to_file

    cm.ctx.events.log_line("http getImgURL " + url)
    response = http_get(url, 5.0, {"If-Modified-Since": format_http_date(fetched), "X-ESL-MAC": mac_hex})
    if response is None:
        cm.ctx.events.ws_err("http " + url + " failed")
        return 0
    if response.status_code == 200:
        temp = cm.fs.path("/temp/temp.jpg")
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(response.content)
        if not jpg_to_file(temp, cm.fs.path(filename), params, cm.renderer.fonts):
            cm.ctx.events.ws_err("invalid jpg")
    elif response.status_code != 304:
        cm.ctx.events.ws_err(f"http {url} {response.status_code}")
    return response.status_code


# --------------------------------------------------------------------------
# 9: RSS
# --------------------------------------------------------------------------
def _fetch_rss(url: str, max_items: int, title_size: int = 255, desc_size: int = 1000) -> list[tuple[str, str]]:
    response = http_get(url, 5.0)
    if response is None or response.status_code != 200:
        return []
    items: list[tuple[str, str]] = []
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        log.warning("rss parse error: %s", exc)
        return []
    atom = "{http://www.w3.org/2005/Atom}"
    for item in root.iter("item"):
        title = (item.findtext("title") or "")[:title_size]
        desc = (item.findtext("description") or "")[:desc_size]
        items.append((title, desc))
        if len(items) >= max_items:
            return items
    if not items:
        for entry in root.iter(atom + "entry"):
            title = (entry.findtext(atom + "title") or "")[:title_size]
            desc = (entry.findtext(atom + "summary") or entry.findtext(atom + "content") or "")[:desc_size]
            items.append((title, desc))
            if len(items) >= max_items:
                break
    return items


def get_rss_feed(cm: "ContentManager", filename: str, url: str, title: str, record: "TagRecord",
                 params: ImageParams) -> bool:
    r = cm.renderer
    cm.ctx.events.ws_log("get rss feed")
    loc = r.get_template(9, record.hw_type)
    spr = r.init_sprite(params.width, params.height, params)
    if is_empty_or_null(title):
        title = "RSS feed"
    title_tpl = loc.get("title", [])
    r.draw_string(spr, title, tpl_int(title_tpl, 0), tpl_int(title_tpl, 1), tpl_str(title_tpl, 2), TL_DATUM,
                  TFT_BLACK, tpl_int(title_tpl, 3))
    line = loc.get("line", [])
    posx = tpl_int(line, 0)
    posy = tpl_int(line, 1)
    articles = _fetch_rss(url, as_int(loc.get("items"), 0) or 0)
    desc_tpl = loc.get("desc", [])
    lineheight = as_float(tpl(desc_tpl, 3, 1.0), 1.0)
    for article_title, article_desc in articles:
        article_title = remove_html(replace_html_entities(article_title))
        if not is_empty_or_null(article_title):
            posx, posy = r.draw_textbox(spr, article_title, posx, posy, params.width - 2 * posx, 100,
                                        tpl_str(line, 2), TFT_BLACK)
        if article_desc is not None and tpl_str(desc_tpl, 2) != "":
            article_desc = remove_html(replace_html_entities(article_desc))
            if not is_empty_or_null(article_desc):
                posy += tpl_int(desc_tpl, 0)
                posx, posy = r.draw_textbox(spr, article_desc, posx, posy, params.width - 2 * posx, 100,
                                            tpl_str(desc_tpl, 2), TFT_BLACK, TFT_WHITE, lineheight)
                posy += tpl_int(desc_tpl, 1)
        else:
            posy += tpl_int(desc_tpl, 1)
    sprite_to_file(spr, cm.fs.path(filename), params)
    return True


# --------------------------------------------------------------------------
# 10: QR code
# --------------------------------------------------------------------------
def draw_qr(cm: "ContentManager", filename: str, qr_content: str, title: str, record: "TagRecord",
            params: ImageParams) -> None:
    import qrcode
    import qrcode.constants

    r = cm.renderer
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0, box_size=1)
    qr.add_data(qr_content or "")
    qr.make(fit=True)
    modules = qr.get_matrix()
    size = len(modules)

    loc = r.get_template(10, record.hw_type)
    spr = r.init_sprite(params.width, params.height, params)
    title_tpl = loc.get("title", [])
    r.draw_string(spr, title, tpl_int(title_tpl, 0), tpl_int(title_tpl, 1), tpl_str(title_tpl, 2), TC_DATUM,
                  TFT_BLACK, tpl_int(title_tpl, 3))
    pos = loc.get("pos", [])
    pos_y = tpl_int(pos, 1)
    dotsize = int((params.height - pos_y) / size) if size else 1
    xpos = tpl_int(pos, 0) - dotsize * size // 2
    ypos = pos_y + (params.height - pos_y - dotsize * size) // 2
    for y in range(size):
        for x in range(size):
            if modules[y][x]:
                spr.fill_rect(xpos + x * dotsize, ypos + y * dotsize, dotsize, dotsize, TFT_BLACK)
    sprite_to_file(spr, cm.fs.path(filename), params)


# --------------------------------------------------------------------------
# 16: buienradar
# --------------------------------------------------------------------------
def draw_buienradar(cm: "ContentManager", filename: str, cfg: dict, record: "TagRecord", params: ImageParams) -> int:
    r = cm.renderer
    refresh = 60
    cm.ctx.events.ws_log("get buienradar")
    get_location(cm, cfg)
    lat, lon = cfg_str(cfg, "#lat"), cfg_str(cfg, "#lon")
    response = http_get(f"https://gadgets.buienradar.nl/data/raintext/?lat={lat}&lon={lon}", 5.0)
    if response is None or response.status_code != 200:
        cm.ctx.events.ws_err("Buitenradar http " + ("0" if response is None else str(response.status_code)))
        return refresh

    loc = r.get_template(16, record.hw_type)
    spr = r.init_sprite(params.width, params.height, params)
    text = response.text
    location = loc.get("location", [])
    r.draw_string(spr, cfg_str(cfg, "location"), tpl_int(location, 0), tpl_int(location, 1), tpl_str(location, 2))
    bars = loc.get("bars", [])
    cols = loc.get("cols", [])
    cols0, cols1, cols2, cols3 = tpl_int(cols, 0), tpl_int(cols, 1), tpl_int(cols, 2), tpl_str(cols, 3)
    bars0, bars1, bars2 = tpl_int(bars, 0), tpl_int(bars, 1), tpl_int(bars, 2)
    factor = bars1 / 111.0
    for i in range(0, params.width, 4):
        for y in (1, 20, 29, 39, 49, 55, 59):
            spr.draw_pixel(i, int(bars1 - (y * factor)), TFT_BLACK)
    title_tpl = loc.get("title", [])
    r.draw_string(spr, "Buienradar", tpl_int(title_tpl, 0), tpl_int(title_tpl, 1), tpl_str(title_tpl, 2))
    for i in range(24):
        start = i * 10
        value = as_int(text[start:start + 3].strip() or 0)
        timestring = text[start + 4:start + 9]
        minutes = as_int(timestring[3:].strip() or 0)
        value = max(70, min(180, value))
        if value > 70:
            if i < 12:
                refresh = 10
            elif refresh > 10:
                refresh = 20
        value -= 70
        spr.fill_rect(i * cols2 + bars0, int(bars1 - (value * factor)), bars2, int(value * factor),
                      params.highlight_color if value > 50 else TFT_BLACK)
        if minutes % 15 == 0:
            r.draw_string(spr, timestring, i * cols2 + cols0, cols1, cols3)
    sprite_to_file(spr, cm.fs.path(filename), params)
    return refresh


# --------------------------------------------------------------------------
# 21: AP info
# --------------------------------------------------------------------------
def draw_ap_info(cm: "ContentManager", filename: str, cfg: dict, record: "TagRecord", params: ImageParams) -> None:
    r = cm.renderer
    if record.hw_type == SOLUM_SEG_UK:
        params.symbols = 0
        params.segments = ""
        return
    loc = r.get_template(21, record.hw_type)
    spr = r.init_sprite(params.width, params.height, params)
    drawer = ElementDrawer(r, cm.fs, spr, params)
    drawer.draw_all(loc if isinstance(loc, list) else [])
    sprite_to_file(drawer.spr, cm.fs.path(filename), params)


# --------------------------------------------------------------------------
# 26: timestamp (button presses)
# --------------------------------------------------------------------------
def draw_timestamp(cm: "ContentManager", filename: str, cfg: dict, record: "TagRecord", params: ImageParams,
                   preload: list[tuple[str, int]]) -> None:
    """``drawTimestamp``.  ``preload`` receives (filename, special_type) pairs
    for button images that the manager has to queue with ``preloadImage``."""
    from .. import protocol as P
    from ..protocol import WAKEUP_REASON_BUTTON1, WAKEUP_REASON_BUTTON2

    r = cm.renderer
    lang = cm.ctx.language
    now = now_epoch()
    spr = r.init_sprite(params.width, params.height, params)
    hexmac = P.mac_to_hex(record.mac)

    if not cfg.get("#init"):
        for suffix, special_type, button in (("-2", 17, "button1"), ("-3", 16, "button2")):
            filename2 = f"/temp/{hexmac}{suffix}.raw"
            spr.fill_rect(0, 0, spr.width, spr.height, TFT_WHITE)
            r.draw_string(spr, cfg_str(cfg, button), spr.width // 2, 40, "calibrib30.vlw", TC_DATUM, TFT_BLACK)
            r.draw_string(spr, "Well done!", spr.width // 2, 90, "calibrib30.vlw", TC_DATUM, TFT_BLACK)
            sprite_to_file(spr, cm.fs.path(filename2), params)
            preload.append((filename2, special_type))
        cfg["#init"] = "1"

    offsety = 0
    buttony = 145
    spr.fill_rect(0, 0, spr.width, spr.height, TFT_WHITE)
    if params.rotate == 2:
        offsety = 25
        buttony = 10
        w, h = spr.width, spr.height
        spr.fill_triangle(w - 27, h - 160, w - 37, h - 160, w - 32, h - 165, TFT_BLACK)
        spr.fill_triangle(w - 127, h - 160, w - 117, h - 160, w - 122, h - 165, TFT_BLACK)
        r.draw_string(spr, cfg_str(cfg, "button1"), w - 32, buttony, "calibrib16.vlw", TC_DATUM, TFT_BLACK)
        r.draw_string(spr, cfg_str(cfg, "button2"), w - 122, buttony, "calibrib16.vlw", TC_DATUM, TFT_BLACK)
    else:
        spr.fill_triangle(27, 160, 37, 160, 32, 165, TFT_BLACK)
        spr.fill_triangle(127, 160, 117, 160, 122, 165, TFT_BLACK)
        r.draw_string(spr, cfg_str(cfg, "button1"), 32, buttony, "calibrib16.vlw", TC_DATUM, TFT_BLACK)
        r.draw_string(spr, cfg_str(cfg, "button2"), 122, buttony, "calibrib16.vlw", TC_DATUM, TFT_BLACK)
    r.draw_string(spr, cfg_str(cfg, "title"), spr.width // 2, 10 + offsety, "calibrib30.vlw", TC_DATUM, TFT_BLACK)
    spr.draw_line(0, 40 + offsety, spr.width, 40 + offsety, TFT_BLACK)

    mode = cfg_int(cfg, "mode")
    if record.wakeup_reason == WAKEUP_REASON_BUTTON2:
        cfg["last1"] = now
        if mode == 0:
            cfg["last2"] = cfg_str(cfg, "button1")
    elif record.wakeup_reason == WAKEUP_REASON_BUTTON1:
        if mode == 0:
            cfg["last1"] = now
            cfg["last2"] = cfg_str(cfg, "button2")
        else:
            cfg["last2"] = now

    next_action = cfg_int(cfg, "nextaction")
    date_format = lang.date_format_long + " %H:%M"
    timestamp = cfg_int(cfg, "last1")
    date_string1 = time.strftime(date_format, time.localtime(timestamp)) if timestamp else "never"

    if mode == 0:
        r.draw_string(spr, "last:", 10, 50 + offsety, "calibrib16.vlw", TL_DATUM, TFT_BLACK)
        r.draw_string(spr, date_string1, spr.width // 2, 50 + offsety, "bahnschrift30.vlw", TC_DATUM, TFT_BLACK)
        r.draw_string(spr, cfg_str(cfg, "last2"), spr.width // 2, 80 + offsety, "bahnschrift30.vlw", TC_DATUM,
                      TFT_BLACK)
        if next_action > 0 and timestamp > 0:
            timestamp += next_action * 24 * 3600
            if timestamp < record.nextupdate:
                record.nextupdate = timestamp
            date_string1 = time.strftime(lang.date_format_long, time.localtime(timestamp))
            r.draw_string(spr, "next:", 10, 115 + offsety, "calibrib16.vlw", TL_DATUM, TFT_BLACK)
            r.draw_string(spr, date_string1, 50, 115 + offsety, "calibrib16.vlw", TL_DATUM,
                          params.highlight_color if timestamp < now else TFT_BLACK)
    else:
        r.draw_string(spr, cfg_str(cfg, "button1"), 10, 50 + offsety, "calibrib16.vlw", TL_DATUM, TFT_BLACK)
        r.draw_string(spr, date_string1, 20, 67 + offsety, "fonts/bahnschrift20", TL_DATUM, TFT_BLACK)
        if next_action > 0 and timestamp > 0:
            timestamp += next_action * 24 * 3600
            if timestamp < record.nextupdate:
                record.nextupdate = timestamp
            date_string1 = time.strftime(lang.date_format_long, time.localtime(timestamp))
            r.draw_string(spr, "next", 200, 50 + offsety, "calibrib16.vlw", TL_DATUM, TFT_BLACK)
            r.draw_string(spr, date_string1, 210, 67 + offsety, "fonts/bahnschrift20", TL_DATUM,
                          params.highlight_color if timestamp < now else TFT_BLACK)
        timestamp2 = cfg_int(cfg, "last2")
        date_string2 = time.strftime(date_format, time.localtime(timestamp2)) if timestamp2 else "never"
        r.draw_string(spr, cfg_str(cfg, "button2"), 10, 90 + offsety, "calibrib16.vlw", TL_DATUM, TFT_BLACK)
        r.draw_string(spr, date_string2, 20, 107 + offsety, "fonts/bahnschrift20", TL_DATUM, TFT_BLACK)
        if next_action > 0 and timestamp2 > 0:
            timestamp2 += next_action * 24 * 3600
            date_string2 = time.strftime(lang.date_format_long, time.localtime(timestamp2))
            r.draw_string(spr, "next", 200, 90 + offsety, "calibrib16.vlw", TL_DATUM, TFT_BLACK)
            r.draw_string(spr, date_string2, 210, 107 + offsety, "fonts/bahnschrift20", TL_DATUM,
                          params.highlight_color if timestamp2 < now else TFT_BLACK)
    sprite_to_file(spr, cm.fs.path(filename), params)
