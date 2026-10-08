"""Palette mapping and dithering (``spr2color`` in ``makeimage.cpp``).

The input is a Pillow RGB image in the orientation the content was drawn
in; the output is one palette index per pixel in *buffer* orientation
(after applying the tag rotation and ``rotatebuffer``).

``dither`` modes:

* 0 – nearest palette colour
* 1 – Burkes error diffusion (default)
* 2 – ordered pattern between the two closest colours
"""

from __future__ import annotations

import logging
from typing import Sequence

from PIL import Image

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]


def rotate_for_buffer(image: Image.Image, rotate: int, rotatebuffer: int) -> tuple[Image.Image, int, int]:
    """Return (rotated image, bufw, bufh) as ``spr2color`` would read it."""
    sw, sh = image.size
    if rotatebuffer % 2:
        rotate = (rotate + 3) % 4
        rotate = (rotate + (rotatebuffer - 1)) % 4
        bufw, bufh = sh, sw
    else:
        rotate = (rotate + rotatebuffer) % 4
        bufw, bufh = sw, sh
    if rotate == 1:
        rotated = image.transpose(Image.Transpose.ROTATE_270)
    elif rotate == 2:
        rotated = image.transpose(Image.Transpose.ROTATE_180)
    elif rotate == 3:
        rotated = image.transpose(Image.Transpose.ROTATE_90)
    else:
        rotated = image
    if rotated.size != (bufw, bufh):
        # readPixel() returns 0 (black) outside the sprite
        canvas = Image.new("RGB", (bufw, bufh), (0, 0, 0))
        canvas.paste(rotated, (0, 0))
        rotated = canvas
    return rotated, bufw, bufh


def quantize_rgb565(image: Image.Image) -> Image.Image:
    """Round colours the way a 16 bit TFT_eSprite stores them."""
    lut_r = [((v & 0xF8) | (v >> 5)) for v in range(256)]
    lut_g = [((v & 0xFC) | (v >> 6)) for v in range(256)]
    lut_b = lut_r
    return image.convert("RGB").point(lut_r + lut_g + lut_b)


def color_distance(c1: RGB, c2: RGB, er: int, eg: int, eb: int) -> float:
    r_diff = c1[0] + er - c2[0]
    g_diff = c1[1] + eg - c2[1]
    b_diff = c1[2] + eb - c2[2]
    if abs(c1[0] - c1[1]) < 20 and abs(c1[2] - c1[1]) < 20:
        if abs(c2[0] - c2[1]) > 20 or abs(c2[2] - c2[1]) > 20:
            return 4294967295.0  # never pick a colour for a grey pixel
    return 3 * r_diff * r_diff + 5.47 * g_diff * g_diff + 1.53 * b_diff * b_diff


def _distance_u32(c1: RGB, c2: RGB, er: int, eg: int, eb: int) -> int:
    """``colorDistance`` returns uint32_t: the float is truncated."""
    value = color_distance(c1, c2, er, eg, eb)
    return int(value) & 0xFFFFFFFF


def find_closest_colors(pixel: RGB, palette: Sequence[RGB]) -> tuple[int, int, float, float]:
    closest = second = -1
    closest_dist = second_dist = float("inf")
    for index, color in enumerate(palette):
        dist = float(color_distance(pixel, color, 0, 0, 0))
        if dist < closest_dist:
            second, second_dist = closest, closest_dist
            closest, closest_dist = index, dist
        elif dist < second_dist:
            second, second_dist = index, dist
    if closest != -1 and second != -1:
        def rgb_value(c: RGB) -> int:
            return (c[0] << 16) | (c[1] << 8) | c[2]

        if rgb_value(palette[second]) > rgb_value(palette[closest]):
            closest, second = second, closest
            closest_dist, second_dist = second_dist, closest_dist
    return closest, second, closest_dist, second_dist


def _c_div(value: int, divisor: int) -> int:
    """C style integer division (truncation toward zero)."""
    quotient = abs(value) // divisor
    return quotient if value >= 0 else -quotient


def dither_indices_python(image: Image.Image, palette: Sequence[RGB], dither: int, num_colors: int) -> bytearray:
    """Map ``image`` (already in buffer orientation) to palette indices."""
    bufw, bufh = image.size
    pixels = image.tobytes()
    out = bytearray(bufw * bufh)
    pal = [tuple(int(v) for v in c) for c in palette]
    num_colors = min(num_colors, len(pal))

    err_old = [[0, 0, 0] for _ in range(bufw + 4)]
    err_new = [[0, 0, 0] for _ in range(bufw + 4)]

    for y in range(bufh):
        for cell in err_new:
            cell[0] = cell[1] = cell[2] = 0
        row_base = y * bufw * 3
        for x in range(bufw):
            offset = row_base + x * 3
            color = (pixels[offset], pixels[offset + 1], pixels[offset + 2])
            best = 0
            if dither == 2:
                c1, c2, d1, d2 = find_closest_colors(color, pal)
                total = d1 + d2
                weight = d1 / total if total else 0.0
                if weight <= 0.03:
                    best = c1
                elif weight < 0.30:
                    best = c2 if (y % 2 and ((y // 2 + x) % 2)) else c1
                elif weight < 0.70:
                    best = c2 if (x + y) % 2 else c1
                elif weight < 0.97:
                    best = c1 if ((y % 2 and ((y // 2 + x) % 2)) % 2) else c2
                else:
                    best = c2
            else:
                eo = err_old[x]
                best_distance = _distance_u32(color, pal[0], eo[0], eo[1], eo[2])
                for i in range(1, num_colors):
                    if best_distance == 0:
                        break
                    distance = _distance_u32(color, pal[i], eo[0], eo[1], eo[2])
                    if distance < best_distance:
                        best_distance = distance
                        best = i
            out[y * bufw + x] = best

            if dither == 1:
                eo = err_old[x]
                chosen = pal[best]
                er = color[0] + eo[0] - chosen[0]
                eg = color[1] + eo[1] - chosen[1]
                eb = color[2] + eo[2] - chosen[2]
                largest = max(abs(er), abs(eg), abs(eb))
                if largest > 255:
                    scale = 255.0 / largest
                    er = int(er * scale)
                    eg = int(eg * scale)
                    eb = int(eb * scale)
                q4 = (_c_div(er, 4), _c_div(eg, 4), _c_div(eb, 4))
                q8 = (_c_div(er, 8), _c_div(eg, 8), _c_div(eb, 8))
                q16 = (_c_div(er, 16), _c_div(eg, 16), _c_div(eb, 16))
                n = err_new
                n[x][0] += q4[0]
                n[x][1] += q4[1]
                n[x][2] += q4[2]
                if x > 0:
                    n[x - 1][0] += q8[0]
                    n[x - 1][1] += q8[1]
                    n[x - 1][2] += q8[2]
                if x > 1:
                    n[x - 2][0] += q16[0]
                    n[x - 2][1] += q16[1]
                    n[x - 2][2] += q16[2]
                n[x + 1][0] += q8[0]
                n[x + 1][1] += q8[1]
                n[x + 1][2] += q8[2]
                o = err_old
                o[x + 1][0] += q4[0]
                o[x + 1][1] += q4[1]
                o[x + 1][2] += q4[2]
                n[x + 2][0] += q16[0]
                n[x + 2][1] += q16[1]
                n[x + 2][2] += q16[2]
                o[x + 2][0] += q8[0]
                o[x + 2][1] += q8[1]
                o[x + 2][2] += q8[2]
        # memcpy(error_bufferold, error_buffernew, bufw * sizeof(Error))
        for x in range(bufw):
            err_old[x][0], err_old[x][1], err_old[x][2] = err_new[x]
    return out


def dither_indices(image: Image.Image, palette: Sequence[RGB], dither: int, num_colors: int) -> bytearray:
    """Dither with the native helper when available, else in Python."""
    from . import native

    result = native.dither_indices(image, palette, dither, num_colors)
    if result is not None:
        return result
    return dither_indices_python(image, palette, dither, num_colors)
