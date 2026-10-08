"""Content type 27: day-ahead electricity prices (``getDayAheadFeed``)."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..image.encode import ImageParams, sprite_to_file
from ..image.sprite import TC_DATUM, TFT_BLACK, TFT_LIGHTGREY, TL_DATUM, TR_DATUM, get_color
from ..tagdb import HwType
from .util import (
    arduino_float_str, as_float, as_int, c_round, cfg_float, cfg_int, cfg_str, http_get, tpl, tpl_int,
    tpl_str,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..tagdb import TagRecord
    from .manager import ContentManager

log = logging.getLogger(__name__)

DAYAHEAD_URL = ("https://script.google.com/macros/s/AKfycbwMmeGAaPrWzVZrESSpmPmD--O132PzW_acnBsuEottKNATTqCRn6h8zN0Yts7S56ggsg"
                "/exec?country=")

TARGET_BARWIDTH = 5.0
MIN_LABEL_SPACING = 30
DASH_LENGTH = 3
GAP_LENGTH = 2
STEM_WIDTH = 3
ARROW_WIDTH = 8
ARROW_HEIGHT = 6
STEM_HEIGHT = 10
GAP_AFTER_ARROW = 2


def get_percentile_color(prices: list[float], price: float, hw: HwType) -> int:
    if hw.bpp in (3, 4):
        colors = ["blue", "green", "yellow", "orange", "red"]
        boundaries = [20.0, 50.0, 70.0, 90.0]
    else:
        colors = ["black", "darkgray", "pink", "red"]
        boundaries = [40.0, 80.0, 90.0]
        if hw.highlight_color == 3:
            colors[2] = "brown"
            colors[3] = "yellow"
    color_index = len(colors) - 1
    count = len(prices)
    for i, boundary in enumerate(boundaries):
        if price < prices[int(count * boundary / 100.0)]:
            color_index = i
            break
    return get_color(colors[max(0, min(color_index, len(colors) - 1))])


def map_double(value: float, from_min: float, from_max: float, to_min: int, to_max: int) -> int:
    span = from_max - from_min
    if span == 0:
        return to_min
    return c_round((value - from_min) / span * (to_max - to_min) + to_min)


def calculate_y_axis_scale(price_min: float, price_max: float, divisions: int) -> tuple[float, float, float]:
    divisions = max(divisions, 1)
    span = price_max - price_min
    step = span / divisions
    if step <= 0:
        step = 1.0
    magnitude = 10 ** math.floor(math.log10(step))
    significant = step / magnitude
    rounded = 10.0
    for threshold in (1.0, 2.0, 5.0, 10.0):
        if significant <= threshold:
            rounded = threshold
            break
    rounded_step = rounded * magnitude
    new_range = rounded_step * divisions
    min_y = math.floor(price_min / rounded_step) * rounded_step
    return min_y, min_y + new_range, rounded_step


@dataclass
class _Averaged:
    time: int
    price: float
    hour: int
    minute: int


def _data_range(times: list[int], now: int, avg_factor: int, target_interval: int,
                bar_area_width: int) -> tuple[int, int, int, int]:
    size = len(times)
    target_start = now - 3600
    start = 0
    for i in range(size - 1, -1, -1):
        if times[i] <= target_start:
            start = i
            break
    available = size - start
    averaged = available // avg_factor
    end = size
    if averaged > bar_area_width:
        end = min(start + bar_area_width * avg_factor, size)
        available = end - start
        averaged = available // avg_factor
    if avg_factor > 1:
        for i in range(start, end):
            minute = time.localtime(times[i]).tm_min
            if target_interval == 30 and minute in (0, 30):
                start = i
                break
            if target_interval == 60 and minute == 0:
                start = i
                break
        available = end - start
        averaged = available // avg_factor
    return start, end, averaged, available


def _backfill(start: int, end: int, averaged: int, avg_factor: int, bar_area_width: int,
              target_barwidth: float) -> tuple[int, int, int, float, float]:
    added = 0
    historic = start
    barwidth_before = bar_area_width / averaged if averaged else 0.0
    if historic > 0:
        pixels_per_bar = bar_area_width / averaged
        spacing = 1 if pixels_per_bar >= 3.0 else 0
        current_barwidth = pixels_per_bar - spacing
        if current_barwidth > target_barwidth:
            target_bars = int(bar_area_width / (target_barwidth + 1.0))
            bars_to_add = target_bars - averaged
            if bars_to_add > 0:
                added = min(bars_to_add * avg_factor, historic)
                start -= added
                if start < 0:
                    added += start
                    start = 0
                averaged = (end - start) // avg_factor
        else:
            current_int = int(pixels_per_bar)
            while historic > 0:
                test_bars = averaged + 1
                if int(bar_area_width / test_bars) >= current_int:
                    averaged = test_bars
                    historic -= avg_factor
                    added += avg_factor
                    start -= avg_factor
                    if start < 0:
                        added += start
                        averaged = end // avg_factor
                        start = 0
                        break
                else:
                    break
    barwidth_after = bar_area_width / averaged if averaged else 0.0
    return start, averaged, added, barwidth_before, barwidth_after


def _cheapest_blocks(avg: list[_Averaged], block_samples: int, now: int) -> tuple[tuple[int, int], tuple[int, int]]:
    none = (-1, -1)
    n = len(avg)
    if block_samples <= 0 or block_samples > n:
        return none, none
    candidates = []
    for i in range(0, n - block_samples + 1):
        if avg[i].time < now:
            continue
        total = sum(avg[i + j].price for j in range(block_samples))
        candidates.append((total, i, i + block_samples - 1))
    candidates.sort(key=lambda c: c[0])
    if not candidates:
        return none, none
    block1 = (candidates[0][1], candidates[0][2])
    block2 = none
    for _total, start, end in candidates[1:]:
        if not (start <= block1[1] and end >= block1[0]):
            block2 = (start, end)
            break
    return block1, block2


def get_day_ahead_feed(cm: "ContentManager", filename: str, cfg: dict, record: "TagRecord",
                       params: ImageParams) -> bool:
    r = cm.renderer
    events = cm.ctx.events
    events.ws_log("get dayahead prices")
    loc = r.get_template(27, record.hw_type)
    now = int(time.time())

    response = http_get(DAYAHEAD_URL + cfg_str(cfg, "country"), 10.0)
    if response is None or response.status_code != 200:
        events.ws_err("getDayAhead http error " + ("0" if response is None else str(response.status_code)))
        return False
    try:
        doc = response.json()
    except ValueError as exc:
        events.ws_err(str(exc))
        doc = []
    if not isinstance(doc, list) or not doc:
        events.ws_err("No data in dayahead feed")
        return False
    doc = [item if isinstance(item, dict) else {} for item in doc]
    times = [as_int(item.get("time")) for item in doc]
    raw_prices = [as_float(item.get("price")) for item in doc]
    size = len(doc)

    spr = r.init_sprite(params.width, params.height, params)

    native_interval = 15
    if size >= 2:
        native_interval = (times[1] - times[0]) // 60 or 15
    target_interval = cfg_int(cfg, "interval") or native_interval
    avg_factor = max(target_interval // native_interval, 1)
    bars = loc.get("bars", [])
    bar_area_width = tpl_int(bars, 1)

    start, end, averaged, _available = _data_range(times, now, avg_factor, target_interval, bar_area_width)
    if averaged == 0:
        events.ws_err("Not enough data for selected interval")
        return False
    start, averaged, added, bw_before, bw_after = _backfill(start, end, averaged, avg_factor, bar_area_width,
                                                            TARGET_BARWIDTH)
    events.ws_log(f"Day-ahead bar sizing: future_raw={end - (start + added)} (incl now), historic_raw={start + added}, "
                  f"barwidth_before={bw_before:.1f}px, added_historic={added}, barwidth_after={bw_after:.1f}px "
                  f"(target={TARGET_BARWIDTH:.1f}px)")

    units = cfg_int(cfg, "units") or 1
    tariff_tax = cfg_float(cfg, "tarifftax")
    tariff_string = cfg_str(cfg, "tariffkwh")
    tariff_array: list = []
    if tariff_string.startswith("["):
        try:
            parsed = json.loads(tariff_string)
            if isinstance(parsed, list):
                tariff_array = parsed
        except json.JSONDecodeError:
            log.warning("Error in tariffkwh array")

    def tariff_for(epoch: int) -> float:
        if len(tariff_array) == 24:
            return as_float(tariff_array[time.localtime(epoch).tm_hour])
        return cfg_float(cfg, "tariffkwh")

    def full_price(index: int) -> float:
        return (raw_prices[index] / 10 + tariff_for(times[index])) * (1 + tariff_tax / 100) / units

    avg: list[_Averaged] = []
    for i in range(averaged):
        block_time = times[start + i * avg_factor]
        total = 0.0
        for j in range(avg_factor):
            idx = start + i * avg_factor + j
            if idx >= end:
                break
            total += full_price(idx)
        tm = time.localtime(block_time)
        avg.append(_Averaged(block_time, total / avg_factor, tm.tm_hour, tm.tm_min))
    n = averaged

    raw_current_price = float("nan")
    raw_hour = raw_minute = 0
    closest = None
    for i in range(size):
        if times[i] > now:
            continue
        diff = now - times[i]
        if closest is None or diff < closest:
            closest = diff
            raw_current_price = full_price(i)
            tm = time.localtime(times[i])
            raw_hour, raw_minute = tm.tm_hour, tm.tm_min

    prices = sorted(point.price for point in avg)
    min_price, max_price = prices[0], prices[-1]
    yaxis = loc.get("yaxis", [])
    min_price, _max_price_scaled, step = calculate_y_axis_scale(min_price, max_price, tpl_int(yaxis, 2))
    # The firmware keeps the real maximum for the mapping and only replaces the minimum.
    y_axis_x = tpl_int(yaxis, 1)
    y_axis_y = tpl_int(yaxis, 3) | 9
    bar_bottom = tpl_int(bars, 3)
    bar_height_px = tpl_int(bars, 2)

    value = min_price
    while value <= max_price:
        y = map_double(value, min_price, max_price, spr.height - bar_bottom, spr.height - bar_bottom - bar_height_px)
        spr.draw_line(0, y, spr.width, y, TFT_BLACK)
        if tpl(yaxis, 0, None):
            label = f"{value * units:.1f}" if max_price * units < 10 else str(int(value * units))
            r.draw_string(spr, label, y_axis_x, y - y_axis_y, tpl_str(yaxis, 0), TL_DATUM, TFT_BLACK)
        value += step

    available_width = bar_area_width
    pixels_per_bar = available_width // n
    if pixels_per_bar >= 3:
        bar_spacing = 1
        barwidth = pixels_per_bar - 1
    else:
        bar_spacing = 0
        barwidth = pixels_per_bar

    arrow_y = tpl_int(bars, 4) if len(bars) >= 5 else 0
    bar_x = tpl_int(bars, 0)
    price_now = float("nan")
    show_current = cfg_str(cfg, "showcurr") != "0"

    max_labels = max(available_width // MIN_LABEL_SPACING, 1)
    label_interval = max((n + max_labels - 1) // max_labels, 1)
    labels_per_hour = 60 // target_interval if target_interval else 0
    if labels_per_hour > 0:
        hours_per_label = max((label_interval + labels_per_hour - 1) // labels_per_hour, 1)
        label_interval = hours_per_label * labels_per_hour
        if n // label_interval > max_labels:
            label_interval = (n + max_labels - 1) // max_labels

    cheap_hours = cfg_float(cfg, "cheapblock", 3.0) if cfg.get("cheapblock") else 3.0
    block_samples = int(cheap_hours * labels_per_hour)
    block1, block2 = _cheapest_blocks(avg, block_samples, now)

    pointer_x = -1
    current_hour = current_minute = 0
    current_bar_height = 0
    baseline_y = spr.height - bar_bottom
    x_axis_height = 20
    for block in (block1, block2):
        if block[0] >= 0 and block[1] >= 0:
            rect_x = bar_x + block[0] * (barwidth + bar_spacing)
            rect_width = (block[1] - block[0] + 1) * (barwidth + bar_spacing)
            spr.fill_rect(rect_x, baseline_y + 1, rect_width, x_axis_height - 1, TFT_LIGHTGREY)

    time_tpl = loc.get("time", [])
    skip_ticks = spr.height < 150
    for i, point in enumerate(avg):
        bar_color = get_percentile_color(prices, point.price, params.hwdata)
        this_bar_h = map_double(point.price, min_price, max_price, 0, bar_height_px)
        spr.fill_rect(bar_x + i * (barwidth + bar_spacing), spr.height - bar_bottom - this_bar_h, barwidth, this_bar_h,
                      bar_color)
        label_x = bar_x + i * (barwidth + bar_spacing) + barwidth // 2
        tick_y = spr.height - bar_bottom
        if i % label_interval == 0 and tpl(time_tpl, 0, None):
            if not skip_ticks:
                spr.draw_line(label_x, tick_y, label_x, tick_y + 4, TFT_BLACK)
            r.draw_string(spr, str(point.hour), label_x, tick_y + (3 if skip_ticks else 6), tpl_str(time_tpl, 0),
                          TC_DATUM, TFT_BLACK)
        elif point.minute == 0 and not skip_ticks:
            spr.draw_line(label_x, tick_y, label_x, tick_y + 3, get_color("darkgray"))
        elif point.minute == 30 and not skip_ticks:
            spr.draw_pixel(label_x, tick_y + 1, get_color("darkgray"))
        if math.isnan(price_now) and show_current:
            if point.time <= now < point.time + target_interval * 60:
                pointer_x = bar_x + i * (barwidth + bar_spacing) + barwidth // 2
                price_now = raw_current_price
                current_hour, current_minute = raw_hour, raw_minute
                current_bar_height = this_bar_h

    if pointer_x >= 0 and show_current:
        spr.fill_rect(pointer_x - STEM_WIDTH // 2, 5 + arrow_y, STEM_WIDTH, STEM_HEIGHT, params.highlight_color)
        spr.fill_triangle(pointer_x - ARROW_WIDTH // 2, 15 + arrow_y, pointer_x + ARROW_WIDTH // 2, 15 + arrow_y,
                          pointer_x, 15 + arrow_y + ARROW_HEIGHT, params.highlight_color)
        line_start = 21 + arrow_y + GAP_AFTER_ARROW
        bar_top = baseline_y - current_bar_height
        y = line_start
        while y < bar_top - 2:
            spr.draw_line(pointer_x, y, pointer_x, min(y + DASH_LENGTH, bar_top - 2), TFT_BLACK)
            y += DASH_LENGTH + GAP_LENGTH
        spr.draw_line(pointer_x, baseline_y, pointer_x, spr.height, TFT_BLACK)

    if show_current:
        clock = f"{current_hour}:{current_minute:02d}"
        price_text = (arduino_float_str(price_now) if not math.isnan(price_now) else "nan") + "/kWh"
        head_font = tpl_str(loc.get("head", []), 0)
        if barwidth < 5:
            r.draw_string(spr, clock, spr.width // 2, 5, "calibrib16.vlw", TC_DATUM, TFT_BLACK, 30)
            r.draw_string(spr, price_text, spr.width // 2, 25, head_font, TC_DATUM, TFT_BLACK, 30)
        else:
            r.draw_string(spr, clock, bar_x, 5, head_font, TL_DATUM, TFT_BLACK, 30)
            r.draw_string(spr, price_text, spr.width - bar_x, 5, head_font, TR_DATUM, TFT_BLACK, 30)

    sprite_to_file(spr, cm.fs.path(filename), params)
    return True
