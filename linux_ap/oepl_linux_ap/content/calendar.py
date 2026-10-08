"""Content type 11: Google Apps Script calendar (list and week view)."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import TYPE_CHECKING

from ..image.encode import ImageParams, sprite_to_file
from ..image.sprite import TC_DATUM, TFT_BLACK, TFT_WHITE, TL_DATUM, TR_DATUM, get_color
from .util import as_int, as_str, c_wday, cfg_str, http_get, is_empty_or_null, now_epoch, tpl, tpl_int, tpl_str

if TYPE_CHECKING:  # pragma: no cover
    from ..tagdb import TagRecord
    from .manager import ContentManager

log = logging.getLogger(__name__)


def epoch_to_display(utc: int, date_format_short: str) -> str:
    """``epoch_to_display``: time of day today, otherwise the short date."""
    local = time.localtime(utc)
    now = time.localtime()
    if (local.tm_year < now.tm_year
            or (local.tm_year == now.tm_year and local.tm_mon < now.tm_mon)
            or (local.tm_year == now.tm_year and local.tm_mon == now.tm_mon and local.tm_mday < now.tm_mday)
            or (local.tm_hour == 0 and local.tm_min == 0)
            or utc - time.time() >= 86400):
        return time.strftime(date_format_short, local)[:5]
    return time.strftime("%H:%M", local)


def _midnight_epoch() -> int:
    today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(today.timestamp())


def _constrain(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def get_cal_feed(cm: "ContentManager", filename: str, cfg: dict, record: "TagRecord", params: ImageParams) -> bool:
    r = cm.renderer
    lang = cm.ctx.language
    cm.ctx.events.ws_log("get calendar")
    loc = r.get_template(11, record.hw_type)
    url = cfg_str(cfg, "apps_script_url") + "?days=" + as_str(loc.get("days"))
    now = now_epoch()
    date_string = time.strftime(lang.date_format_long, time.localtime(now))

    response = http_get(url, 10.0)
    if response is None or response.status_code != 200:
        cm.ctx.events.ws_err("getCalFeed http error " + ("0" if response is None else str(response.status_code)))
        return False
    try:
        doc = response.json()
    except ValueError as exc:
        cm.ctx.events.ws_err(str(exc))
        doc = []
    if not isinstance(doc, list):
        doc = []

    if as_int(loc.get("rotate")) == 1:
        params.width, params.height = params.height, params.width
        params.rotatebuffer = 1 - (params.rotatebuffer % 2)
    spr = r.init_sprite(params.width, params.height, params)

    mode = as_int(loc.get("mode"))
    if mode == 0:
        title = cfg_str(cfg, "title")
        if is_empty_or_null(title):
            title = "Calendar"
        title_tpl = loc.get("title", [])
        date_tpl = loc.get("date", [])
        line = loc.get("line", [])
        red = loc.get("red", [])
        r.draw_string(spr, title, tpl_int(title_tpl, 0), tpl_int(title_tpl, 1), tpl_str(title_tpl, 2), TL_DATUM,
                      TFT_BLACK)
        r.draw_string(spr, date_string, tpl_int(date_tpl, 0), tpl_int(date_tpl, 1), tpl_str(title_tpl, 2), TR_DATUM,
                      TFT_BLACK)
        count = min(len(doc), as_int(loc.get("items")))
        for i in range(count):
            obj = doc[i] if isinstance(doc[i], dict) else {}
            event_title = as_str(obj.get("title"))
            start = as_int(obj.get("start"))
            end = as_int(obj.get("end"))
            y = tpl_int(line, 1) + i * tpl_int(line, 2)
            display = epoch_to_display(start, lang.date_format_short)
            if start <= now < end:
                spr.fill_rect(tpl_int(red, 0), tpl_int(red, 1) + i * tpl_int(line, 2), tpl_int(red, 2),
                              tpl_int(red, 3), params.highlight_color)
                r.draw_string(spr, display, tpl_int(line, 0), y, tpl_str(line, 3), TL_DATUM, TFT_WHITE, 0,
                              params.highlight_color)
                r.draw_string(spr, event_title, tpl_int(line, 4), y, tpl_str(line, 3), TL_DATUM, TFT_WHITE, 0,
                              params.highlight_color)
            else:
                r.draw_string(spr, display, tpl_int(line, 0), y, tpl_str(line, 3), TL_DATUM, TFT_BLACK, 0, TFT_WHITE)
                r.draw_string(spr, event_title, tpl_int(line, 4), y, tpl_str(line, 3), TL_DATUM, TFT_BLACK, 0,
                              TFT_WHITE)
    elif mode == 1:
        _draw_week_view(cm, spr, loc, doc, params)

    sprite_to_file(spr, cm.fs.path(filename), params)
    return True


def _event_colors(loc: dict, calendar_id: int) -> tuple[int, int, int]:
    background, border, textcolor = TFT_WHITE, TFT_BLACK, TFT_BLACK
    colors1 = loc.get("colors1")
    if isinstance(colors1, list) and len(colors1) > calendar_id >= 0:
        background = get_color(colors1[calendar_id])
        border = get_color(tpl(loc.get("colors2", []), calendar_id, ""))
        text = tpl(loc.get("colors3", []), calendar_id, None)
        if text:
            textcolor = get_color(text)
    return background, border, textcolor


def _draw_week_view(cm: "ContentManager", spr, loc: dict, doc: list, params: ImageParams) -> None:
    r = cm.renderer
    lang = cm.ctx.language
    grid = loc.get("gridparam", [])
    midnight = _midnight_epoch()
    cal_width = params.width - 1
    cal_height = params.height
    cal_days = max(as_int(loc.get("days")), 1)
    col_left = tpl_int(grid, 2)
    col_width = (cal_width - col_left) // cal_days
    cal_top = tpl_int(grid, 0)
    cal_bottom = cal_height
    cal_y_offset = tpl_int(grid, 1)
    line_height = tpl_int(grid, 5)
    header_font = tpl_str(grid, 3)
    event_font = tpl_str(grid, 4)

    background_light = get_color("lightgray")
    background_dark = get_color("darkgray")
    if params.hwdata.bpp >= 3:
        background_light = get_color("#BEFAFF")
        background_dark = get_color("#79FAFF")

    for i in range(cal_days):
        day_date = dt.datetime.fromtimestamp(midnight) + dt.timedelta(days=i)
        day_info = day_date.timetuple()
        col_start = col_left + i * col_width
        spr.draw_line(col_start, cal_top, col_start, cal_bottom, TFT_BLACK)
        r.draw_string(spr, f"{lang.days_short[c_wday(day_info)]} {day_info.tm_mday}",
                      col_start + col_width // 2, cal_top, header_font, TC_DATUM, TFT_BLACK)
        weekend = c_wday(day_info) in (0, 6)
        spr.fill_rect(col_start + 1, cal_top + cal_y_offset, col_width - 1, cal_height - 1,
                      background_dark if weekend else background_light)

    col_start = col_left + cal_days * col_width
    spr.draw_line(col_start, cal_top, col_start, cal_bottom, TFT_BLACK)
    spr.draw_line(0, cal_top + cal_y_offset, cal_width, cal_top + cal_y_offset, TFT_BLACK)

    min_hour, max_hour = 9, 17
    events = [item if isinstance(item, dict) else {} for item in doc]
    block = [0] * len(events)
    max_block = 0

    for i, obj in enumerate(events):
        title = as_str(obj.get("title"))
        start = as_int(obj.get("start"))
        end = as_int(obj.get("end"))
        is_all_day = bool(obj.get("isallday"))
        calendar_id = as_int(obj.get("calendar"))
        if not is_all_day:
            start_tm = time.localtime(start)
            if start_tm.tm_hour < min_hour:
                min_hour = start_tm.tm_hour
            end_tm = time.localtime(end)
            if end_tm.tm_hour > max_hour:
                max_hour = end_tm.tm_hour
            if end_tm.tm_min > 1 and end_tm.tm_hour + 1 > max_hour:
                max_hour = end_tm.tm_hour + 1
        else:
            full_day_start = _constrain((start - midnight) // (24 * 3600), 0, cal_days)
            full_day_end = _constrain((end - midnight) // (24 * 3600), 0, cal_days)
            if full_day_start < cal_days:
                line = 1
                for j in range(i):
                    obj2 = events[j]
                    if (start < as_int(obj2.get("end")) and end > as_int(obj2.get("start"))
                            and line == block[j] and bool(obj2.get("isallday"))):
                        line += 1
                event_x = col_left + full_day_start * col_width + 3
                event_y = cal_top + cal_y_offset + (line - 1) * line_height + 3
                background, border, textcolor = _event_colors(loc, calendar_id)
                span = col_width * (full_day_end - full_day_start)
                spr.fill_rect(event_x - 1, event_y - 2, span - 3, line_height - 1, background)
                spr.draw_rect(event_x - 2, event_y - 3, span - 1, line_height + 1, border)
                r.draw_textbox(spr, title, event_x, event_y, span - 3, 15, event_font, textcolor)
                block[i] = line
                max_block = max(max_block, line)

    cal_y_offset += max_block * line_height
    spr.draw_line(0, cal_top + cal_y_offset, cal_width, cal_top + cal_y_offset, TFT_BLACK)
    hour_span = max(max_hour - min_hour, 1)
    hour_height = (cal_height - cal_y_offset - cal_top) // hour_span
    for i in range(hour_span):
        y = cal_top + cal_y_offset + i * hour_height
        spr.draw_line(0, y, col_left + 10, y, TFT_BLACK)
        for j in range(1, cal_days + 1):
            spr.draw_line(col_left + j * col_width - 5, y, col_left + j * col_width + 5, y, TFT_BLACK)
        r.draw_string(spr, str(min_hour + i), col_left - 2, y + 2, header_font, TR_DATUM, TFT_BLACK)
    spr.draw_line(0, cal_top + cal_y_offset + hour_span * hour_height, cal_width,
                  cal_top + cal_y_offset + hour_span * hour_height, TFT_BLACK)

    for i, obj in enumerate(events):
        title = as_str(obj.get("title"))
        start = as_int(obj.get("start"))
        end = as_int(obj.get("end"))
        is_all_day = bool(obj.get("isallday"))
        calendar_id = as_int(obj.get("calendar"))
        if is_all_day:
            continue
        full_day_start = _constrain((start - midnight) // (24 * 3600), 0, cal_days)
        full_day_end = _constrain((end - midnight) // (24 * 3600), 0, cal_days)
        for day in range(full_day_start, full_day_end + 1):
            start_tm = time.localtime(start)
            start_minutes = start_tm.tm_hour * 60 + start_tm.tm_min - min_hour * 60
            duration = (end - start) // 60
            tm_for_label = start_tm
            if day > full_day_start:
                start_minutes = 0
                end_tm = time.localtime(end)
                duration = end_tm.tm_hour * 60 + end_tm.tm_min - min_hour * 60
                tm_for_label = end_tm
            formatted_time = time.strftime("%H:%M", tm_for_label)

            indent = 1
            for j in range(i):
                obj2 = events[j]
                if (start < as_int(obj2.get("end")) and end > as_int(obj2.get("start"))
                        and indent == block[j] and not bool(obj2.get("isallday"))):
                    indent += 1
            block[i] = indent
            event_x = col_left + day * col_width + (indent - 1) * 5
            event_y = cal_top + cal_y_offset + (start_minutes * hour_height // 60)
            background, border, textcolor = _event_colors(loc, calendar_id)
            event_height = duration * hour_height // 60
            spr.fill_rect(event_x + 2, event_y + 1, col_width - 3, event_height - 1, background)
            spr.draw_rect(event_x + 1, event_y, col_width - 1, event_height + 1, border)
            event_x += 2
            event_y += 2
            if day == full_day_start:
                event_title = formatted_time + " " + title
                event_x, event_y = r.draw_textbox(spr, formatted_time, event_x, event_y, col_width - 1,
                                                  event_height - 1, event_font, textcolor, TFT_WHITE, 1)
                event_x += 1
                event_y = cal_top + cal_y_offset + (start_minutes * hour_height // 60) + 2
            else:
                event_title = title
            r.draw_textbox(spr, event_title, event_x, event_y, col_width - 1, event_height - 1, event_font,
                           textcolor, TFT_WHITE, 1)
