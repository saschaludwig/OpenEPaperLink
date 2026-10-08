"""A drawing surface that behaves like the TFT_eSprite used by the firmware.

Colours are RGB565 values (``TFT_*`` constants) everywhere in the content
code; the sprite stores them expanded to 8 bit per channel exactly like the
``Color`` struct in ``makeimage.cpp`` does, so the palette mapping later on
sees the same values the ESP sees.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw

from .dither import quantize_rgb565
from .fonts import FontLibrary, VLWFont

log = logging.getLogger(__name__)

# TFT_eSPI colour constants
TFT_BLACK = 0x0000
TFT_NAVY = 0x000F
TFT_DARKGREEN = 0x03E0
TFT_DARKCYAN = 0x03EF
TFT_MAROON = 0x7800
TFT_PURPLE = 0x780F
TFT_OLIVE = 0x7BE0
TFT_LIGHTGREY = 0xD69A
TFT_DARKGREY = 0x7BEF
TFT_BLUE = 0x001F
TFT_GREEN = 0x07E0
TFT_CYAN = 0x07FF
TFT_RED = 0xF800
TFT_MAGENTA = 0xF81F
TFT_YELLOW = 0xFFE0
TFT_WHITE = 0xFFFF
TFT_ORANGE = 0xFDA0
TFT_GREENYELLOW = 0xB7E0
TFT_PINK = 0xFE19
TFT_BROWN = 0x9A60
TFT_GOLD = 0xFEA0
TFT_SILVER = 0xC618
TFT_SKYBLUE = 0x867D
TFT_VIOLET = 0x915C

# text datums
TL_DATUM = 0
TC_DATUM = 1
TR_DATUM = 2
ML_DATUM = 3
MC_DATUM = 4
MR_DATUM = 5
BL_DATUM = 6
BC_DATUM = 7
BR_DATUM = 8
L_BASELINE = 9
C_BASELINE = 10
R_BASELINE = 11


def color565_to_rgb(value: int) -> tuple[int, int, int]:
    """Expand RGB565 to 8 bit channels (``Color(uint16_t)`` in makeimage)."""
    value &= 0xFFFF
    r = ((value >> 8) & 0xF8) | ((value >> 13) & 0x07)
    g = ((value >> 3) & 0xFC) | ((value >> 9) & 0x03)
    b = ((value << 3) & 0xF8) | ((value >> 2) & 0x07)
    return r, g, b


def rgb_to_color565(r: int, g: int, b: int) -> int:
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def get_color(color) -> int:
    """``getColor()`` from contentmanager.cpp."""
    text = "" if color is None else str(color).strip()
    table = {
        "0": TFT_WHITE, "white": TFT_WHITE,
        "1": TFT_BLACK, "": TFT_BLACK, "black": TFT_BLACK,
        "2": TFT_RED, "red": TFT_RED,
        "3": TFT_YELLOW, "yellow": TFT_YELLOW,
        "4": 0xBDF7, "lightgray": 0xBDF7,
        "5": TFT_DARKGREY, "darkgray": TFT_DARKGREY,
        "6": 0xFBCF, "pink": 0xFBCF,
        "7": 0x8400, "brown": 0x8400,
        "8": TFT_GREEN, "green": TFT_GREEN,
        "9": TFT_BLUE, "blue": TFT_BLUE,
        "10": 0xFBE0, "orange": 0xFBE0,
    }
    if text in table:
        return table[text]
    if len(text) == 7 and text[0] == "#":
        try:
            r = int(text[1:3], 16)
            g = int(text[3:5], 16)
            b = int(text[5:7], 16)
            return rgb_to_color565(r, g, b)
        except ValueError:
            pass
    return TFT_WHITE


# 5x7 GLCD glyphs (column major, LSB = top row) for the timestamp overlay
_GLCD_DIGITS = {
    "0": (0x3E, 0x51, 0x49, 0x45, 0x3E),
    "1": (0x00, 0x42, 0x7F, 0x40, 0x00),
    "2": (0x42, 0x61, 0x51, 0x49, 0x46),
    "3": (0x21, 0x41, 0x45, 0x4B, 0x31),
    "4": (0x18, 0x14, 0x12, 0x7F, 0x10),
    "5": (0x27, 0x45, 0x45, 0x45, 0x39),
    "6": (0x3C, 0x4A, 0x49, 0x49, 0x30),
    "7": (0x01, 0x71, 0x09, 0x05, 0x03),
    "8": (0x36, 0x49, 0x49, 0x49, 0x36),
    "9": (0x06, 0x49, 0x49, 0x29, 0x1E),
    "-": (0x08, 0x08, 0x08, 0x08, 0x08),
    ":": (0x00, 0x36, 0x36, 0x00, 0x00),
    " ": (0x00, 0x00, 0x00, 0x00, 0x00),
}


class Sprite:
    """RGB canvas with TFT_eSPI style primitives."""

    def __init__(self, width: int, height: int, fonts: FontLibrary | None = None,
                 fill: int = TFT_WHITE) -> None:
        self.width = max(int(width), 1)
        self.height = max(int(height), 1)
        self.fonts = fonts or FontLibrary([])
        self.image = Image.new("RGB", (self.width, self.height), color565_to_rgb(fill))
        self.draw = ImageDraw.Draw(self.image)

    # -- basic primitives ------------------------------------------------
    def fill_sprite(self, color: int) -> None:
        self.draw.rectangle((0, 0, self.width - 1, self.height - 1), fill=color565_to_rgb(color))

    def draw_pixel(self, x: int, y: int, color: int) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            self.image.putpixel((int(x), int(y)), color565_to_rgb(color))

    def read_pixel(self, x: int, y: int) -> int:
        if 0 <= x < self.width and 0 <= y < self.height:
            return rgb_to_color565(*self.image.getpixel((int(x), int(y))))
        return 0

    def draw_rect(self, x: int, y: int, w: int, h: int, color: int) -> None:
        if w <= 0 or h <= 0:
            return
        self.draw.rectangle((x, y, x + w - 1, y + h - 1), outline=color565_to_rgb(color))

    def fill_rect(self, x: int, y: int, w: int, h: int, color: int) -> None:
        if w <= 0 or h <= 0:
            return
        self.draw.rectangle((x, y, x + w - 1, y + h - 1), fill=color565_to_rgb(color))

    def draw_round_rect(self, x: int, y: int, w: int, h: int, r: float, color: int) -> None:
        if w <= 0 or h <= 0:
            return
        radius = max(int(r), 0)
        radius = min(radius, (w - 1) // 2, (h - 1) // 2)
        self.draw.rounded_rectangle((x, y, x + w - 1, y + h - 1), radius=radius, outline=color565_to_rgb(color))

    def fill_round_rect(self, x: int, y: int, w: int, h: int, r: float, color: int) -> None:
        if w <= 0 or h <= 0:
            return
        radius = max(int(r), 0)
        radius = min(radius, (w - 1) // 2, (h - 1) // 2)
        self.draw.rounded_rectangle((x, y, x + w - 1, y + h - 1), radius=radius, fill=color565_to_rgb(color))

    def draw_line(self, x0: int, y0: int, x1: int, y1: int, color: int) -> None:
        self.draw.line((x0, y0, x1, y1), fill=color565_to_rgb(color), width=1)

    def draw_fast_hline(self, x: int, y: int, w: int, color: int) -> None:
        if w > 0:
            self.draw.line((x, y, x + w - 1, y), fill=color565_to_rgb(color))

    def draw_fast_vline(self, x: int, y: int, h: int, color: int) -> None:
        if h > 0:
            self.draw.line((x, y, x, y + h - 1), fill=color565_to_rgb(color))

    def draw_circle(self, x: int, y: int, r: float, color: int) -> None:
        radius = int(r)
        if radius < 0:
            return
        self.draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=color565_to_rgb(color))

    def fill_circle(self, x: int, y: int, r: float, color: int) -> None:
        radius = int(r)
        if radius < 0:
            return
        self.draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color565_to_rgb(color))

    def draw_triangle(self, x0, y0, x1, y1, x2, y2, color: int) -> None:
        self.draw.polygon([(x0, y0), (x1, y1), (x2, y2)], outline=color565_to_rgb(color))

    def fill_triangle(self, x0, y0, x1, y1, x2, y2, color: int) -> None:
        self.draw.polygon([(x0, y0), (x1, y1), (x2, y2)], fill=color565_to_rgb(color), outline=color565_to_rgb(color))

    # -- images ------------------------------------------------------------
    def push_image(self, image: Image.Image, x: int = 0, y: int = 0) -> None:
        """Paste a Pillow image (quantised to RGB565) at x/y."""
        self.image.paste(quantize_rgb565(image), (int(x), int(y)))

    def push_jpeg(self, path: Path | str, x: int = 0, y: int = 0) -> bool:
        try:
            with Image.open(path) as source:
                source.load()
                self.push_image(source.convert("RGB"), x, y)
            return True
        except (OSError, ValueError) as exc:
            log.warning("cannot decode %s: %s", path, exc)
            return False

    def rotated(self, angle: int) -> "Sprite":
        """Return a copy rotated clockwise by 90/180/270 degrees (``pushRotated``)."""
        angle %= 360
        if angle == 90:
            image = self.image.transpose(Image.Transpose.ROTATE_270)
        elif angle == 180:
            image = self.image.transpose(Image.Transpose.ROTATE_180)
        elif angle == 270:
            image = self.image.transpose(Image.Transpose.ROTATE_90)
        else:
            image = self.image.copy()
        result = Sprite(image.width, image.height, self.fonts)
        result.image = image
        result.draw = ImageDraw.Draw(result.image)
        return result

    def copy(self) -> "Sprite":
        result = Sprite(self.width, self.height, self.fonts)
        result.image = self.image.copy()
        result.draw = ImageDraw.Draw(result.image)
        return result

    # -- text --------------------------------------------------------------
    def text_width(self, text: str, font_name: str) -> int:
        font = self.fonts.vlw(font_name)
        if font is None:
            return len(text) * 6
        return font.text_width(text)

    def font_height(self, font_name: str) -> int:
        font = self.fonts.vlw(font_name)
        if font is None:
            return 8
        return font.font_height()

    def draw_vlw_string(self, text: str, x: int, y: int, font: VLWFont, datum: int, color: int,
                        bgcolor: int) -> None:
        """``TFT_eSPI::drawString`` with a loaded smooth font."""
        cwidth = font.text_width(text)
        cheight = font.font_height()
        baseline = font.max_ascent
        x, y = int(x), int(y)
        if datum in (TC_DATUM, MC_DATUM, BC_DATUM, C_BASELINE):
            x -= cwidth // 2
        elif datum in (TR_DATUM, MR_DATUM, BR_DATUM, R_BASELINE):
            x -= cwidth
        if datum in (ML_DATUM, MC_DATUM, MR_DATUM):
            y -= cheight // 2
        elif datum in (BL_DATUM, BC_DATUM, BR_DATUM):
            y -= cheight
        elif datum in (L_BASELINE, C_BASELINE, R_BASELINE):
            y -= baseline

        fg = color565_to_rgb(color)
        bg = color565_to_rgb(bgcolor)
        read_background = color == bgcolor
        for glyph, left, top in font.layout(text, x, y):
            if glyph.width == 0 or glyph.height == 0:
                continue
            if left >= self.width or top >= self.height or left + glyph.width <= 0 or top + glyph.height <= 0:
                continue
            mask = font.glyph_mask(glyph)
            fg_layer = Image.new("RGB", mask.size, fg)
            if read_background:
                bg_layer = Image.new("RGB", mask.size, (0, 0, 0))
                region = self.image.crop((left, top, left + glyph.width, top + glyph.height))
                bg_layer.paste(region, (0, 0))
            else:
                bg_layer = Image.new("RGB", mask.size, bg)
            blended = quantize_rgb565(Image.composite(fg_layer, bg_layer, mask))
            coverage = mask.point(lambda alpha: 255 if alpha else 0)
            self.image.paste(blended, (left, top), coverage)

    def draw_ttf_string(self, text: str, x: int, y: int, font_name: str, size: int, datum: int,
                        color: int) -> None:
        """TrueType text: ``size`` is the em size, ``y`` the top of the em box."""
        font = self.fonts.ttf(font_name, size)
        if font is None:
            return
        x, y = int(x), int(y)
        if datum == TC_DATUM:
            x -= font.text_width(text) // 2
        elif datum == TR_DATUM:
            x -= font.text_width(text)
        mask = font.render(text)
        self.image.paste(Image.new("RGB", mask.size, color565_to_rgb(color)), (x, y), mask)

    def draw_glcd_text(self, text: str, x: int, y: int, color: int, bgcolor: int) -> None:
        """Built-in 6x8 font used for the timestamp overlay (``spr->print``)."""
        for char in text:
            columns = _GLCD_DIGITS.get(char, _GLCD_DIGITS[" "])
            for col in range(6):
                bits = columns[col] if col < 5 else 0
                for row in range(8):
                    pixel_on = row < 7 and (bits >> row) & 1
                    self.draw_pixel(x + col, y + row, color if pixel_on else bgcolor)
            x += 6
