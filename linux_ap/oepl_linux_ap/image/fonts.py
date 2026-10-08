"""Font handling for the drawing layer.

Two font kinds are used by the templates in ``tagtypes/*.json``:

* ``*.vlw`` smooth fonts (Processing/TFT_eSPI format: 8 bit alpha bitmaps).
  :class:`VLWFont` mirrors the metrics and placement rules of
  ``TFT_eSPI::loadFont`` / ``drawGlyph`` / ``textWidth`` so that templates
  designed for the ESP32 AP look the same.
* ``*.ttf`` fonts, rendered with Pillow.  The ESP uses a small TrueType
  rasteriser without anti-aliasing where ``size`` is the em size in pixels
  and a space is ``size / 4`` wide; :class:`TTFFont` follows those rules.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Glyph:
    unicode: int
    height: int
    width: int
    x_advance: int
    dy: int  # distance from the top of the glyph to the baseline
    dx: int  # left offset from the cursor
    bitmap_offset: int


class VLWFont:
    """A TFT_eSPI smooth font."""

    def __init__(self, path: Path) -> None:
        self.path = path
        data = path.read_bytes()
        if len(data) < 24:
            raise ValueError(f"{path}: not a VLW font")
        glyph_count, _version, point_size, _unused, ascent, descent = struct.unpack(">6i", data[:24])
        self.glyph_count = glyph_count
        self.point_size = point_size
        self.ascent = ascent
        self.descent = descent
        self.max_ascent = ascent
        self.max_descent = descent
        self.glyphs: dict[int, Glyph] = {}

        offset = 24
        bitmap_ptr = 24 + glyph_count * 28
        for _ in range(glyph_count):
            unicode, height, width, x_advance, dy, dx, _pad = struct.unpack(">7i", data[offset:offset + 28])
            offset += 28
            glyph = Glyph(unicode, height, width, x_advance, dy, dx, bitmap_ptr)
            bitmap_ptr += height * width
            self.glyphs.setdefault(unicode, glyph)
            # loadMetrics(): only printable glyphs influence the line metrics
            if (0x20 < unicode < 0xA0 and unicode != 0x7F) or unicode > 0xFF:
                if dy > self.max_ascent:
                    self.max_ascent = dy
                if height - dy > self.max_descent:
                    self.max_descent = height - dy
        self.y_advance = self.max_ascent + self.max_descent
        self.space_width = (self.ascent + self.descent) * 2 // 7
        self._data = data
        self._masks: dict[int, Image.Image] = {}

    # -- metrics ----------------------------------------------------------
    def text_width(self, text: str) -> int:
        """``TFT_eSPI::textWidth`` for smooth fonts."""
        width = 0
        chars = list(text)
        for index, char in enumerate(chars):
            code = ord(char)
            if code == 0:
                continue
            if code == 0x20:
                width += self.space_width
                continue
            glyph = self.glyphs.get(code)
            if glyph is None:
                width += self.space_width + 1
                continue
            if width == 0 and glyph.dx < 0:
                width -= glyph.dx
            if index < len(chars) - 1:
                width += glyph.x_advance
            else:
                width += glyph.dx + glyph.width
        return width

    def font_height(self) -> int:
        return self.y_advance

    # -- rendering --------------------------------------------------------
    def glyph_mask(self, glyph: Glyph) -> Image.Image:
        mask = self._masks.get(glyph.unicode)
        if mask is None:
            size = glyph.width * glyph.height
            raw = self._data[glyph.bitmap_offset:glyph.bitmap_offset + size]
            if glyph.width == 0 or glyph.height == 0:
                mask = Image.new("L", (max(glyph.width, 1), max(glyph.height, 1)), 0)
            else:
                mask = Image.frombytes("L", (glyph.width, glyph.height), raw)
            self._masks[glyph.unicode] = mask
        return mask

    def layout(self, text: str, x: int, y: int) -> Iterable[tuple[Glyph, int, int]]:
        """Yield (glyph, left, top) placements like ``drawGlyph`` does."""
        cursor_x, cursor_y = x, y
        for char in text:
            code = ord(char)
            if code < 0x21:
                if code == 0x20:
                    cursor_x += self.space_width
                elif code == 0x0A:
                    cursor_x = 0
                    cursor_y += self.y_advance
                continue
            glyph = self.glyphs.get(code)
            if glyph is None:
                cursor_x += self.space_width + 1
                continue
            top = cursor_y + self.max_ascent - glyph.dy
            left = cursor_x + glyph.dx
            yield glyph, left, top
            cursor_x += glyph.x_advance


class TTFFont:
    """TrueType font rendered with Pillow (no anti-aliasing, like the ESP)."""

    def __init__(self, path: Path, size: int) -> None:
        self.path = path
        self.size = max(int(size), 1)
        self.font = ImageFont.truetype(str(path), self.size)

    def text_width(self, text: str) -> int:
        width = 0
        for char in text:
            if char in (" ", "\u3000"):
                width += self.size // 4
            else:
                width += int(self.font.getlength(char))
        return width

    def render(self, text: str) -> Image.Image:
        """Return a 1 bit mask; (0, 0) is the top-left of the em box."""
        ascent, descent = self.font.getmetrics()
        height = max(ascent + descent + 2, 1)
        width = max(self.text_width(text), 1)
        mask = Image.new("1", (width + 2, height), 0)
        draw = ImageDraw.Draw(mask)
        cursor = 0
        for char in text:
            if char in (" ", "\u3000"):
                cursor += self.size // 4
                continue
            draw.text((cursor, 0), char, font=self.font, fill=1, anchor="la")
            cursor += int(self.font.getlength(char))
        return mask


class FontLibrary:
    """Resolves font names used by templates to files and caches them."""

    def __init__(self, search_paths: Iterable[Path]) -> None:
        self.search_paths = [Path(p) for p in search_paths]
        self._vlw: dict[Path, VLWFont] = {}
        self._ttf: dict[tuple[Path, int], TTFFont] = {}

    def resolve(self, name: str) -> Path | None:
        """Map ``/fonts/bahnschrift20`` or ``tahoma9.vlw`` to a real file."""
        name = name.strip()
        if not name:
            return None
        candidates = [name]
        if not name.endswith((".vlw", ".ttf")):
            candidates = [name + ".vlw", name + ".ttf", name]
        for candidate in candidates:
            relative = candidate.lstrip("/")
            if relative.startswith("fonts/"):
                relative = relative[len("fonts/"):]
            for base in self.search_paths:
                path = base / relative
                if path.is_file():
                    return path
            direct = Path(candidate)
            if direct.is_absolute() and direct.is_file():
                return direct
        return None

    def vlw(self, name: str) -> VLWFont | None:
        path = self.resolve(name)
        if path is None or path.suffix.lower() != ".vlw":
            if path is None:
                log.warning("font not found: %s", name)
            return None
        font = self._vlw.get(path)
        if font is None:
            font = VLWFont(path)
            self._vlw[path] = font
        return font

    def ttf(self, name: str, size: int) -> TTFFont | None:
        path = self.resolve(name)
        if path is None or path.suffix.lower() != ".ttf":
            if path is None:
                log.warning("font not found: %s", name)
            return None
        key = (path, int(size))
        font = self._ttf.get(key)
        if font is None:
            font = TTFFont(path, size)
            self._ttf[key] = font
        return font
