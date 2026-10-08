"""Turn a :class:`Sprite` into the on-air image file (``spr2buffer``).

Output formats, selected by ``ImageParams``:

* raw 1 bpp / 2 bpp bit planes (black plane, optional red plane)
* raw 3/4 bpp packed indices, packed 2 bit (``BPP_PACKED_2BIT``)
* zlib: ``u32 total_size`` + zlib stream of (6 byte header + planes)
* Group5: 6 byte header + G5 stream, falling back to raw when it does
  not shrink the image
"""

from __future__ import annotations

import logging
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .. import protocol
from ..tagdb import HwType
from . import g5
from .dither import dither_indices, quantize_rgb565, rotate_for_buffer
from .sprite import TFT_BLACK, TFT_WHITE, Sprite

log = logging.getLogger(__name__)

BPP_PACKED_2BIT = 5

SHORTLUT_DISABLED = 0
SHORTLUT_ONLY_BLACK = 1
SHORTLUT_ALLOWED = 2

EPD_LUT_DEFAULT = 0
EPD_LUT_NO_REPEATS = 1
EPD_LUT_FAST_NO_REDS = 2
EPD_LUT_FAST = 3


@dataclass
class ImageParams:
    """Python counterpart of ``struct imgParam``."""

    hwdata: HwType = field(default_factory=HwType)
    has_red: bool = False
    data_type: int = protocol.DATATYPE_IMG_RAW_1BPP
    dither: int = 2
    bufferbpp: int = 16
    rotate: int = 0
    highlight_color: int = 2
    width: int = 0
    height: int = 0
    rotatebuffer: int = 0
    bpp: int = 0
    segments: str = ""
    symbols: int = 0
    invert: int = 0
    lut: int = EPD_LUT_DEFAULT
    shortlut: int = 0
    preload: bool = False
    preloadtype: int = 0
    preloadlut: int = 0
    zlib: int = 0
    g5: int = 0
    ts_option: int = 0

    @classmethod
    def for_hwtype(cls, hw: HwType) -> "ImageParams":
        params = cls(hwdata=hw)
        params.width = hw.width
        params.height = hw.height
        params.bpp = hw.bpp
        params.rotatebuffer = hw.rotatebuffer
        params.shortlut = hw.shortlut
        return params


def _timestamp_overlay(sprite: Sprite, ts_option: int) -> None:
    """``doTimestamp``: small clock stamp in a corner of the sprite."""
    fmt = "%m-%d %H:%M" if ts_option & 0x8 else "%Y-%m-%d %H:%M"
    text = time.strftime(fmt, time.localtime())
    if ts_option & 0x10:
        char_color, bg_color = TFT_WHITE, TFT_BLACK
    else:
        char_color, bg_color = TFT_BLACK, TFT_WHITE
    position = (ts_option & 0x3) - 1
    ts_x = 1 if position & 2 else sprite.width - len(text) * 6 - 2
    ts_y = 1 if position & 1 else sprite.height - 10
    sprite.draw_rect(ts_x - 1, ts_y - 1, len(text) * 6 + 1, 9, bg_color)
    sprite.draw_glcd_text(text, ts_x, ts_y, char_color, bg_color)


def _palette(params: ImageParams) -> list[tuple[int, int, int]]:
    palette = [tuple(c) for c in params.hwdata.colortable]
    if not palette:
        palette = [(255, 255, 255), (0, 0, 0)]
    if params.invert == 1 and len(palette) > 1:
        palette[0], palette[1] = palette[1], palette[0]
    return palette


def _plane_buffers(indices: bytearray, bufw: int, bufh: int, params: ImageParams) -> tuple[bytes, bytes]:
    """Return (black plane, red plane) for 1/2 bpp tags and set ``has_red``."""
    buffer_size = (bufw * bufh + 7) // 8
    if bufw % 8 == 0:
        # Fast path: rows are byte aligned, so Pillow's 1 bit packing matches
        # the continuous bit stream the firmware produces.
        index_image = Image.frombytes("L", (bufw, bufh), bytes(indices))
        black_image = index_image.point(lambda v: 255 if v in (1, 3) else 0).convert("1", dither=Image.Dither.NONE)
        red_image = index_image.point(lambda v: 255 if v in (2, 3) else 0).convert("1", dither=Image.Dither.NONE)
        black = black_image.tobytes()
        red = red_image.tobytes()
        if any(red):
            params.has_red = True
        return black, red
    black = bytearray(buffer_size)
    red = bytearray(buffer_size)
    has_red = False
    for y in range(bufh):
        row = y * bufw
        for x in range(bufw):
            index = indices[row + x]
            if index == 0:
                continue
            bit = 0x80 >> (x % 8)
            byte_index = (row + x) >> 3
            if index == 1:
                black[byte_index] |= bit
            elif index == 2:
                has_red = True
                red[byte_index] |= bit
            elif index == 3:
                has_red = True
                black[byte_index] |= bit
                red[byte_index] |= bit
    if has_red:
        params.has_red = True
    return bytes(black), bytes(red)


def _packed_buffer(indices: bytearray, bufw: int, bufh: int, bits: int, params: ImageParams) -> bytes:
    """Packed palette indices, MSB first (3/4 bpp and ``BPP_PACKED_2BIT``)."""
    buffer_size = (bufw * bufh * bits + 7) // 8
    out = bytearray(buffer_size)
    bit_offset = 0
    for index in indices:
        byte_index = bit_offset >> 3
        bit_index = bit_offset & 7
        if bit_index + bits <= 8:
            out[byte_index] |= index << (8 - bit_index - bits)
        else:
            spill = bit_index + bits - 8
            out[byte_index] |= index >> spill
            out[byte_index + 1] |= (index & ((1 << spill) - 1)) << (8 - spill)
        bit_offset += bits
        if params.bpp == BPP_PACKED_2BIT and index >= 2:
            params.has_red = True
    return bytes(out)


def _prepare_header(bufw: int, bufh: int, params: ImageParams, buffer_size: int) -> tuple[bytes, int]:
    """``prepareHeader``: 6 byte header and the total uncompressed size.

    ``bufw``/``bufh`` are the physical buffer dimensions as returned by
    ``rotate_for_buffer`` (already swapped for odd ``rotatebuffer``).  The
    firmware's ``prepareHeader`` receives the unrotated sprite size and swaps
    the fields itself, so here the values are written as they are.  The web
    front end decodes G5 with the dimensions from this header.
    """
    header = bytearray(6)
    header[0] = 6
    header[1:3] = bufw.to_bytes(2, "little")
    header[3:5] = bufh.to_bytes(2, "little")
    if params.bpp in (3, 4):
        total = buffer_size * params.bpp + 6
        header[5] = params.bpp
    elif params.has_red and params.bpp > 1:
        total = buffer_size * 2 + 6
        header[5] = 2
    else:
        total = buffer_size + 6
        header[5] = 1
    return bytes(header), total


def _zlib_stream(payload: bytes) -> bytes:
    """Deflate with a 4 kB window so the tag can inflate with little RAM."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 12)
    data = compressor.compress(payload) + compressor.flush()
    # rewriteHeader(): CMF 0x48 (window 4096), FLEVEL 3
    cmf = 0x48
    header = (cmf << 8) | (3 << 6)
    header += 31 - (header % 31)
    return bytes([cmf, header & 0xFF]) + data[2:]


def sprite_to_buffer(sprite: Sprite, params: ImageParams) -> bytes:
    """Encode a sprite; returns the file content for ``prepareDataAvail``."""
    started = time.monotonic()
    if params.ts_option:
        _timestamp_overlay(sprite, params.ts_option)

    rotated, bufw, bufh = rotate_for_buffer(sprite.image, params.rotate, params.rotatebuffer)
    rotated = quantize_rgb565(rotated)
    palette = _palette(params)
    num_colors = 2 if params.bufferbpp == 1 else len(palette)
    indices = dither_indices(rotated, palette, params.dither, num_colors)

    if params.bpp in (1, 2):
        buffer_size = (bufw * bufh + 7) // 8
        black, red = _plane_buffers(indices, bufw, bufh, params)
        use_red = params.has_red and params.bpp > 1
        if params.zlib:
            header, total = _prepare_header(bufw, bufh, params, buffer_size)
            payload = header + black + (red if use_red else b"")
            result = total.to_bytes(4, "little") + _zlib_stream(payload)
        elif params.g5:
            header, _ = _prepare_header(bufw, bufh, params, buffer_size)
            planes = black + red if use_red else black
            width, height = params.width, params.height
            if use_red:
                if params.rotatebuffer % 2:
                    width *= 2
                else:
                    height *= 2
            if params.rotatebuffer % 2:
                g5_width, g5_height = height, width
            else:
                g5_width, g5_height = width, height
            try:
                compressed = g5.encode(g5_width, g5_height, planes, len(planes))
            except g5.G5Error as exc:
                log.info("G5 compression failed (%s), falling back to raw", exc)
                compressed = None
            if compressed is not None and len(compressed) <= len(planes):
                log.debug("G5 compressed %d to %d bytes", len(planes), len(compressed))
                result = header + compressed
            else:
                if compressed is not None:
                    log.info("G5 result larger than raw, falling back to raw")
                params.g5 = 0
                params.data_type = protocol.DATATYPE_IMG_RAW_2BPP if use_red else protocol.DATATYPE_IMG_RAW_1BPP
                result = planes
        else:
            result = black + (red if use_red else b"")
    elif params.bpp in (3, 4, BPP_PACKED_2BIT):
        bits = 2 if params.bpp == BPP_PACKED_2BIT else params.bpp
        result = _packed_buffer(indices, bufw, bufh, bits, params)
    elif params.bpp == 16:
        rgb = rotated.tobytes()
        result = bytearray()
        for offset in range(0, len(rgb), 3):
            r, g, b = rgb[offset], rgb[offset + 1], rgb[offset + 2]
            result += (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)).to_bytes(2, "big")
        result = bytes(result)
    else:
        log.warning("unsupported bpp %d", params.bpp)
        result = b""
    log.debug("encoded %dx%d in %.0f ms (%d bytes)", bufw, bufh, (time.monotonic() - started) * 1000, len(result))
    return result


def sprite_to_file(sprite: Sprite, path: Path | str, params: ImageParams) -> bool:
    data = sprite_to_buffer(sprite, params)
    if not data:
        return False
    Path(path).write_bytes(data)
    return True


def jpg_to_file(input_path: Path | str, output_path: Path | str, params: ImageParams,
                fonts=None) -> bool:
    """``jpg2buffer``: decode an uploaded image and encode it for the tag."""
    try:
        with Image.open(input_path) as source:
            source.load()
            image = source.convert("RGB")
    except (OSError, ValueError) as exc:
        log.warning("invalid image %s: %s", input_path, exc)
        return False
    log.info("image conversion %dx%d", image.width, image.height)
    sprite = Sprite(image.width, image.height, fonts, TFT_WHITE)
    sprite.push_image(image, 0, 0)
    return sprite_to_file(sprite, output_path, params)
