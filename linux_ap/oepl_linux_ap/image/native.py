"""ctypes bindings for the optional native helper library ``liboeplg5``."""

from __future__ import annotations

import ctypes
import logging
import platform
from pathlib import Path
from typing import Sequence

from PIL import Image

log = logging.getLogger(__name__)

_lib = None
_checked = False


def library():
    """Load the shared library once; return ``None`` when unavailable."""
    global _lib, _checked
    if _checked:
        return _lib
    _checked = True
    suffix = ".dylib" if platform.system() == "Darwin" else ".so"
    candidates = [
        Path(__file__).resolve().parents[2] / "native" / f"liboeplg5{suffix}",
        Path("/usr/local/lib") / f"liboeplg5{suffix}",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            lib = ctypes.CDLL(str(path))
        except OSError as exc:
            log.warning("cannot load %s: %s", path, exc)
            continue
        lib.oepl_g5_encode.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                                       ctypes.c_char_p, ctypes.c_int]
        lib.oepl_g5_encode.restype = ctypes.c_int
        if hasattr(lib, "oepl_dither"):
            lib.oepl_dither.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
            lib.oepl_dither.restype = ctypes.c_int
        _lib = lib
        log.info("using native image helpers from %s", path)
        return lib
    log.info("native image helpers not found, using pure Python (slower)")
    return None


def dither_indices(image: Image.Image, palette: Sequence[tuple[int, int, int]], dither: int,
                   num_colors: int) -> bytearray | None:
    lib = library()
    if lib is None or not hasattr(lib, "oepl_dither"):
        return None
    width, height = image.size
    rgb = image.convert("RGB").tobytes()
    pal = bytes(v for color in palette for v in color[:3])
    out = ctypes.create_string_buffer(width * height)
    rc = lib.oepl_dither(width, height, rgb, pal, len(palette), num_colors, dither, out)
    if rc != 0:
        return None
    return bytearray(out.raw)
