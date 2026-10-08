"""JSON drawing elements (``drawElement`` / ``rotateBuffer``).

Templates and JSON content use arrays such as ``{"text": [x, y, "txt",
"font", "color", align, size, "bgcolor"]}``; this module renders them onto a
:class:`Sprite`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..image.encode import ImageParams
from ..image.sprite import TFT_WHITE, Sprite, get_color
from ..storage import ContentFS
from .drawing import Renderer
from .util import as_float, as_int, as_str, tpl, tpl_int, tpl_str

log = logging.getLogger(__name__)


class ElementDrawer:
    """Draws a list of JSON elements, tracking the current orientation."""

    def __init__(self, renderer: Renderer, fs: ContentFS, spr: Sprite, params: ImageParams) -> None:
        self.renderer = renderer
        self.fs = fs
        self.spr = spr
        self.params = params
        self.current_orientation = 0

    def draw_all(self, elements) -> None:
        if isinstance(elements, dict):
            elements = [elements]
        if not isinstance(elements, list):
            return
        for element in elements:
            if isinstance(element, dict):
                try:
                    self.draw_element(element)
                except Exception as exc:  # noqa: BLE001 - one bad element must not kill the image
                    log.warning("cannot draw element %s: %s", element, exc)

    def rotate_buffer(self, rotation: int) -> None:
        """``rotateBuffer``: rotate the canvas while drawing."""
        rotation %= 4
        if rotation == self.current_orientation:
            return
        step = (self.current_orientation - rotation + 4) % 4
        if step == 2:
            self.spr = self.spr.rotated(180)
        else:
            angle = 270 if step == 3 else 90
            self.spr = self.spr.rotated(angle)
            self.params.rotatebuffer = 1 - (self.params.rotatebuffer % 2)
        self.current_orientation = rotation

    def draw_element(self, element: dict) -> None:
        r = self.renderer
        spr = self.spr
        if isinstance(element.get("text"), list):
            arr = element["text"]
            align = tpl_int(arr, 5)
            size = tpl_int(arr, 6)
            bgcolor_str = tpl_str(arr, 7)
            bgcolor = get_color(bgcolor_str) if bgcolor_str else TFT_WHITE
            r.draw_string(spr, tpl_str(arr, 2), tpl_int(arr, 0), tpl_int(arr, 1), tpl_str(arr, 3), align,
                          get_color(tpl_str(arr, 4)), size, bgcolor)
        elif isinstance(element.get("textbox"), list):
            arr = element["textbox"]
            lineheight = as_float(tpl(arr, 7, 1.0), 1.0)
            if lineheight == 0:
                lineheight = 1.0
            r.draw_textbox(spr, tpl_str(arr, 4), tpl_int(arr, 0), tpl_int(arr, 1), tpl_int(arr, 2), tpl_int(arr, 3),
                           tpl_str(arr, 5), get_color(tpl_str(arr, 6)), TFT_WHITE, lineheight, tpl_int(arr, 8))
        elif isinstance(element.get("box"), list):
            arr = element["box"]
            x, y, w, h = tpl_int(arr, 0), tpl_int(arr, 1), tpl_int(arr, 2), tpl_int(arr, 3)
            spr.fill_rect(x, y, w, h, get_color(tpl_str(arr, 4)))
            if len(arr) >= 7:
                for i in range(tpl_int(arr, 6)):
                    spr.draw_rect(x + i, y + i, w - 2 * i, h - 2 * i, get_color(tpl_str(arr, 5)))
        elif isinstance(element.get("rbox"), list):
            arr = element["rbox"]
            x, y, w, h, radius = (tpl_int(arr, 0), tpl_int(arr, 1), tpl_int(arr, 2), tpl_int(arr, 3),
                                  tpl_int(arr, 4))
            spr.fill_round_rect(x, y, w, h, radius, get_color(tpl_str(arr, 5)))
            if len(arr) >= 8:
                color = get_color(tpl_str(arr, 6))
                for i in range(tpl_int(arr, 7)):
                    spr.draw_round_rect(x + i, y + i, w - 2 * i, h - 2 * i, radius - i / 1.41, color)
                    if i > 0:
                        spr.draw_round_rect(x + i - 1, y + i, w - 2 * i + 2, h - 2 * i, radius - i / 1.41, color)
        elif isinstance(element.get("line"), list):
            arr = element["line"]
            spr.draw_line(tpl_int(arr, 0), tpl_int(arr, 1), tpl_int(arr, 2), tpl_int(arr, 3),
                          get_color(tpl_str(arr, 4)))
        elif isinstance(element.get("triangle"), list):
            arr = element["triangle"]
            spr.fill_triangle(tpl_int(arr, 0), tpl_int(arr, 1), tpl_int(arr, 2), tpl_int(arr, 3), tpl_int(arr, 4),
                              tpl_int(arr, 5), get_color(tpl_str(arr, 6)))
        elif isinstance(element.get("circle"), list):
            arr = element["circle"]
            x, y, radius = tpl_int(arr, 0), tpl_int(arr, 1), tpl_int(arr, 2)
            spr.fill_circle(x, y, radius, get_color(tpl_str(arr, 3)))
            if len(arr) >= 6:
                color = get_color(tpl_str(arr, 4))
                for i in range(tpl_int(arr, 5)):
                    spr.draw_circle(x, y, radius - i, color)
                    if i > 0:
                        spr.draw_circle(x, y, radius - i - 0.5, color)
        elif isinstance(element.get("image"), list):
            arr = element["image"]
            filename = as_str(tpl(arr, 0, ""))
            if not filename.startswith("/"):
                filename = "/" + filename
            try:
                path: Path = self.fs.path(filename)
            except ValueError:
                return
            if not spr.push_jpeg(path, tpl_int(arr, 1), tpl_int(arr, 2)):
                self.renderer.ctx.events.ws_err("invalid jpg")
        elif "rotate" in element and not isinstance(element["rotate"], (list, dict)):
            self.rotate_buffer(as_int(element["rotate"]))
