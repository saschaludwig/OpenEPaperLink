"""Text and sprite helpers used by all content types.

Ports of ``drawString``, ``drawTextBox``, ``initSprite``, ``replaceVariables``
and ``processFontPath`` from ``contentmanager.cpp``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ..image.encode import ImageParams
from ..image.fonts import FontLibrary
from ..image.sprite import (
    TC_DATUM, TFT_BLACK, TFT_WHITE, TL_DATUM, TR_DATUM, Sprite,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..state import AppContext

log = logging.getLogger(__name__)

FONT_BUILTIN = 1
FONT_TTF = 2
FONT_VLW = 3


def process_font_path(font: str) -> tuple[int, str]:
    """``processFontPath``: classify a font name and normalise its path."""
    if font == "":
        return FONT_VLW, font
    if font in ("glasstown_nbp_tf", "7x14_tf", "t0_14b_tf"):
        return FONT_BUILTIN, font
    if "/" not in font:
        font = "/fonts/" + font
    if not font.startswith("/"):
        font = "/" + font
    if font.endswith(".vlw"):
        font = font[:-4]
    if font.endswith(".ttf"):
        return FONT_TTF, font
    return FONT_VLW, font


class Renderer:
    """Drawing helpers bound to the application context."""

    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.fonts = FontLibrary(ctx.settings.font_search_paths())

    # -- variables -------------------------------------------------------
    def replace_variables(self, text: str) -> str:
        """``replaceVariables``: substitute ``{name}`` with varDB entries."""
        if "{" not in text:
            self.ctx.db.vars.set("ap_time", time.strftime("%H:%M:%S"), False)
            return text
        self.ctx.db.vars.set("ap_time", time.strftime("%H:%M:%S"), False)
        result = []
        index = 0
        length = len(text)
        while index < length:
            open_idx = text.find("{", index)
            if open_idx == -1:
                result.append(text[index:])
                break
            close_idx = text.find("}", open_idx + 1)
            if close_idx == -1:
                result.append(text[index:])
                break
            result.append(text[index:open_idx])
            name = text[open_idx + 1:close_idx]
            value = self.ctx.db.vars.get(name, None)
            result.append("-" if value is None else value)
            index = close_idx + 1
        return "".join(result)

    # -- sprites ---------------------------------------------------------
    def init_sprite(self, width: int, height: int, params: ImageParams | None = None) -> Sprite:
        """``initSprite``: a white 16 bit canvas."""
        return Sprite(width, height, self.fonts, TFT_WHITE)

    # -- text ------------------------------------------------------------
    def draw_string(self, spr: Sprite, content, posx, posy, font: str = "", align: int = TL_DATUM,
                    color: int = TFT_BLACK, size: int = 0, bgcolor: int = TFT_WHITE) -> None:
        """``drawString``: VLW or TrueType text with datum alignment."""
        content = self.replace_variables("" if content is None else str(content))
        posx = int(posx or 0)
        posy = int(posy or 0)
        font = "" if font is None else str(font)
        size = int(size or 0)

        # backwards compatibility with old templates
        if font.startswith("fonts/calibrib"):
            try:
                calibri_size = int(font[14:])
            except ValueError:
                calibri_size = 0
            if calibri_size not in (30, 16):
                font = "Signika-SB.ttf"
                size = calibri_size
        if font == "glasstown_nbp_tf":
            font = "tahoma9.vlw"
            posy -= 8
        if font == "7x14_tf":
            font = "REFSAN12.vlw"
            posy -= 10
        if font == "t0_14b_tf":
            font = "calibrib16.vlw"
            posy -= 11

        kind, path = process_font_path(font)
        if kind == FONT_TTF:
            spr.draw_ttf_string(content, posx, posy, path, size, align, color)
        elif kind == FONT_VLW:
            vlw = self.fonts.vlw(path) if path else None
            if vlw is None:
                vlw = self.fonts.vlw("tahoma9")
            if vlw is None:
                log.warning("no font available for %r", font)
                return
            spr.draw_vlw_string(content, posx, posy, vlw, align, color, bgcolor)

    def draw_textbox(self, spr: Sprite, content: str, posx: int, posy: int, boxwidth: int, boxheight: int,
                     font: str, color: int = TFT_BLACK, bgcolor: int = TFT_WHITE, lineheight: float = 1.0,
                     align: int = TL_DATUM) -> tuple[int, int]:
        """``drawTextBox``: word wrapped text; returns the updated (posx, posy)."""
        content = self.replace_variables("" if content is None else str(content))
        kind, path = process_font_path("" if font is None else str(font))
        if kind == FONT_TTF:
            log.info("truetype font not implemented for drawTextBox")
            return posx, posy
        vlw = self.fonts.vlw(path) if path else None
        if vlw is None:
            vlw = self.fonts.vlw("tahoma9")
        if vlw is None:
            return posx, posy

        length = len(content)
        start = 0
        start_y = posy
        y_advance = vlw.font_height()
        while start < length and posy + y_advance <= start_y + boxheight:
            end = start
            has_space = False
            while end < length and vlw.text_width(content[start:end + 1]) <= boxwidth and content[end] != "\n":
                if content[end] in (" ", "-"):
                    has_space = True
                end += 1
            while (end < length and end > start and has_space and content[end - 1] not in (" ", "-")
                   and content[end] != "\n"):
                end -= 1
            spr.draw_vlw_string(content[start:end], posx, posy, vlw, align, color, bgcolor)
            posy += int(y_advance * lineheight)
            if end < length and content[end] == "\n":
                end += 1
            start = end
            while start < length and content[start] == " ":
                start += 1
        return posx, posy

    def stamp_time(self, spr: Sprite) -> None:
        text = time.strftime("%Y-%m-%d  %H:%M:%S ")
        self.draw_string(spr, text, spr.width - 1, 12, "glasstown_nbp_tf", TR_DATUM, TFT_BLACK)

    # -- templates -------------------------------------------------------
    def get_template(self, content_id: int, hw_type: int):
        """``getTemplate``: the per-content-type layout from ``tagtypes``."""
        template = self.ctx.db.tagtypes.get_template(content_id, hw_type)
        return template if template is not None else {}


__all__ = ["Renderer", "process_font_path", "TC_DATUM", "TL_DATUM", "TR_DATUM"]
