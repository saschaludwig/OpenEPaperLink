"""Group5 1 bpp compression (``g5enc.inl``).

Two implementations produce identical output:

* :func:`encode_native` loads ``native/liboeplg5.so`` (built from the
  encoder in ``ESP32_AP-Flasher/src/g5``) through ``ctypes``.
* :func:`encode_python` is a straight port of the same algorithm and is used
  when the shared library is missing.

Both expect an MSB-first 1 bpp bitmap whose rows are ``width // 8`` bytes,
the same (slightly odd) row stride the ESP firmware uses.
"""

from __future__ import annotations

import ctypes
import logging

from . import native

log = logging.getLogger(__name__)

MAX_IMAGE_FLIPS = 640

G5_SUCCESS = 0
G5_ENCODE_COMPLETE = 4
G5_DATA_OVERFLOW = 7
G5_MAX_FLIPS_EXCEEDED = 8


class G5Error(Exception):
    pass


# --------------------------------------------------------------------------
# native library
# --------------------------------------------------------------------------
def encode_native(width: int, height: int, data: bytes, out_size: int | None = None) -> bytes | None:
    lib = native.library()
    if lib is None:
        return None
    # The ESP passes the *input* size as output limit; the encoder stops with
    # G5_DATA_OVERFLOW 32 bytes before it so an incompressible image falls
    # back to raw.  The buffer itself is larger to be safe.
    limit = len(data) if out_size is None else out_size
    out = ctypes.create_string_buffer(limit + 16384)
    rc = lib.oepl_g5_encode(width, height, bytes(data), out, limit)
    if rc < 0:
        raise G5Error(f"G5 encode failed, rc={-rc}")
    return out.raw[:rc]


# --------------------------------------------------------------------------
# pure Python port
# --------------------------------------------------------------------------
_BITCOUNT = bytes(
    [0] * 128 + [1] * 64 + [2] * 32 + [3] * 16 + [4] * 8 + [5] * 4 + [6] * 2 + [7, 8]
)

# code, length for V(-3) .. V(3)
_VTABLE = ((3, 7), (3, 6), (3, 3), (1, 1), (2, 3), (2, 6), (2, 7))

HORIZ_SHORT_SHORT = 0
HORIZ_SHORT_LONG = 1
HORIZ_LONG_SHORT = 2
HORIZ_LONG_LONG = 3


class _BitWriter:
    __slots__ = ("out", "acc", "nbits")

    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.nbits = 0

    def insert(self, code: int, length: int) -> None:
        self.acc = (self.acc << length) | (code & ((1 << length) - 1))
        self.nbits += length
        while self.nbits >= 32:
            self.nbits -= 32
            self.out += ((self.acc >> self.nbits) & 0xFFFFFFFF).to_bytes(4, "big")
            self.acc &= (1 << self.nbits) - 1

    def flush(self) -> bytes:
        # G5ENCFlushBits: emit whole bytes, then always one more byte.
        while self.nbits >= 8:
            self.nbits -= 8
            self.out.append((self.acc >> self.nbits) & 0xFF)
            self.acc &= (1 << self.nbits) - 1
        self.out.append((self.acc << (8 - self.nbits)) & 0xFF)
        self.acc = 0
        self.nbits = 0
        return bytes(self.out)


def _encode_line_flips(row: bytes, xsize: int) -> list[int]:
    """``G5ENCEncodeLine``: convert pixels into run-end positions."""
    flips: list[int] = []
    limit = MAX_IMAGE_FLIPS - 4
    xborder = xsize
    count = (xsize + 7) >> 3
    cbits = 8
    length = 0
    x = 0
    pos = 0
    c = row[pos] if pos < len(row) else 0
    pos += 1
    count -= 1
    black = False
    broke = False
    while count >= 0:
        if len(flips) >= limit:
            raise G5Error("max flips exceeded")
        if not black:
            i = _BITCOUNT[c]
            length += i
            c = (c << i) & 0xFF
            cbits -= i
            if cbits <= 0:
                length += cbits
                cbits = 8
                c = row[pos] if pos < len(row) else 0
                pos += 1
                count -= 1
                continue
            c = (~c) & 0xFF
            xborder -= length
            if xborder < 0:
                length += xborder
                broke = True
                break
            x += length
            flips.append(x)
            length = 0
            black = True
        else:
            i = _BITCOUNT[c]
            length += i
            c = (c << i) & 0xFF
            cbits -= i
            if cbits <= 0:
                length += cbits
                cbits = 8
                c = row[pos] if pos < len(row) else 0
                pos += 1
                c = (~c) & 0xFF
                count -= 1
                if count < 0:
                    broke = True
                    break
                continue
            c = (~c) & 0xFF
            xborder -= length
            if xborder < 0:
                length += xborder
                broke = True
                break
            x += length
            flips.append(x)
            length = 0
            black = False
    del broke
    x += length
    if len(flips) >= limit:
        raise G5Error("max flips exceeded")
    flips.extend((x, x, x, x))
    return flips


def encode_python(width: int, height: int, data: bytes, out_size: int | None = None) -> bytes:
    bw = _BitWriter()
    xsize = width
    ihlen = width.bit_length()
    ref = [xsize] * MAX_IMAGE_FLIPS
    stride = width // 8
    high_water = (len(data) if out_size is None else out_size) - 32
    for y in range(height):
        row = data[y * stride:y * stride + ((width + 7) >> 3) + 1]
        cur_list = _encode_line_flips(row, xsize)
        cur = cur_list + [xsize] * (MAX_IMAGE_FLIPS - len(cur_list))
        a0 = 0
        icur = iref = 0
        while a0 < xsize:
            b2 = ref[iref + 1]
            a1 = cur[icur]
            if b2 < a1:
                a0 = b2
                iref += 2
                bw.insert(1, 4)
            else:
                dx = ref[iref] - a1
                if dx > 3 or dx < -3:
                    bw.insert(1, 3)
                    run1 = cur[icur] - a0
                    run2 = cur[icur + 1] - cur[icur]
                    if run1 < 8:
                        if run2 < 8:
                            bw.insert(HORIZ_SHORT_SHORT, 2)
                            bw.insert(run1, 3)
                            bw.insert(run2, 3)
                        else:
                            bw.insert(HORIZ_SHORT_LONG, 2)
                            bw.insert(run1, 3)
                            bw.insert(run2, ihlen)
                    else:
                        if run2 < 8:
                            bw.insert(HORIZ_LONG_SHORT, 2)
                            bw.insert(run1, ihlen)
                            bw.insert(run2, 3)
                        else:
                            bw.insert(HORIZ_LONG_LONG, 2)
                            bw.insert(run1, ihlen)
                            bw.insert(run2, ihlen)
                    a0 = cur[icur + 1]
                    if a0 != xsize:
                        icur += 2
                        while ref[iref] != xsize and ref[iref] <= a0:
                            iref += 2
                else:
                    code, length = _VTABLE[dx + 3]
                    bw.insert(code, length)
                    a0 = a1
                    if a0 != xsize:
                        if iref != 0:
                            iref -= 2
                        iref += 1
                        icur += 1
                        while ref[iref] <= a0 and ref[iref] != xsize:
                            iref += 2
        ref = cur
        # The C code checks the high-water mark after encoding each line.
        if len(bw.out) >= high_water:
            raise G5Error("G5 encode failed, rc=7")
    return bw.flush()


def encode(width: int, height: int, data: bytes, out_size: int | None = None) -> bytes:
    """Compress with the native library when available, else in Python.

    Raises :class:`G5Error` when the result would exceed ``out_size`` (the
    caller then falls back to the raw bitmap, like the firmware).
    """
    result = encode_native(width, height, data, out_size)
    if result is not None:
        return result
    return encode_python(width, height, data, out_size)
