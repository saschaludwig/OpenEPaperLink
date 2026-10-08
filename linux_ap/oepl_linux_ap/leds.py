"""Status RGB LED (``leds.cpp``).

The Yellow AP carries a single WS2812B driven by the ESP32-S3 through
FastLED.  Linux cannot bit-bang the 800 kHz protocol reliably, so the LED
data line is connected to SPI0 MOSI of the NanoPi and every WS2812 bit is
sent as three SPI bits at 2.4 MHz (``0`` -> ``100``, ``1`` -> ``110``).
``/dev/spidevX.Y`` is driven directly with ``ioctl``/``write``; no extra
Python package is needed.

The animation logic mirrors ``ledTask``: an instruction queue (fade to a
colour, hold, optionally re-queue for repeating patterns) and a "breathing"
idle animation in the colour of the current AP state.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from .state import AppContext

log = logging.getLogger(__name__)

Color = tuple[int, int, int]

# FastLED ``CRGB::`` named colours used by the firmware (HTML colour values).
BLACK: Color = (0x00, 0x00, 0x00)
WHITE: Color = (0xFF, 0xFF, 0xFF)
RED: Color = (0xFF, 0x00, 0x00)
GREEN: Color = (0x00, 0x80, 0x00)
BLUE: Color = (0x00, 0x00, 0xFF)
DARK_BLUE: Color = (0x00, 0x00, 0x8B)
YELLOW: Color = (0xFF, 0xFF, 0x00)
YELLOW_GREEN: Color = (0x9A, 0xCD, 0x32)
ORANGE: Color = (0xFF, 0xA5, 0x00)
AQUA: Color = (0x00, 0xFF, 0xFF)

# ``setAPstate`` colour map indexed by AP_STATE_* (0..6)
STATE_COLORS: tuple[Color, ...] = (ORANGE, GREEN, BLUE, YELLOW, AQUA, RED, YELLOW_GREEN)

# ``showColorPattern`` timing (ms) – black / c1 / black / c2 / black / c3
PATTERN_LENGTHS = (600, 120, 200, 120, 200, 120)

TICK_SECONDS = 0.02

# spidev ioctl request numbers (linux/spi/spidev.h)
_SPI_IOC_WR_MODE = 0x40016B01
_SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
_SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04

# >= 50 us of low level before and after the data (one byte = 3.3 us at 2.4 MHz)
RESET_BYTES = 24


# ----------------------------------------------------------------------
# colour helpers (FastLED semantics)
# ----------------------------------------------------------------------
def blend(color_a: Color, color_b: Color, amount_of_b: int) -> Color:
    """``blend(a, b, amount)``: 0 gives ``a``, 255 gives ``b``."""
    amount_of_b = max(0, min(255, amount_of_b))
    amount_of_a = 255 - amount_of_b
    return tuple((a * amount_of_a + b * amount_of_b + 127) // 255 for a, b in zip(color_a, color_b))  # type: ignore[return-value]


def maximize_brightness(color: Color, limit: int = 255) -> Color:
    """``CRGB::maximizeBrightness``: scale so that the brightest channel equals ``limit``."""
    peak = max(color)
    if peak == 0:
        return color
    return tuple(min(255, c * limit // peak) for c in color)  # type: ignore[return-value]


def scale(color: Color, brightness: int) -> Color:
    """Global brightness like ``FastLED.setBrightness``."""
    brightness = max(0, min(255, brightness))
    return tuple(c * brightness // 255 for c in color)  # type: ignore[return-value]


def encode_ws2812(color: Color, order: str = "GRB") -> bytes:
    """Encode one pixel as 9 SPI bytes (3 SPI bits per WS2812 bit)."""
    channels = {"R": color[0], "G": color[1], "B": color[2]}
    bits = 0
    for name in order:
        value = channels[name]
        for i in range(7, -1, -1):
            bits = (bits << 3) | (0b110 if (value >> i) & 1 else 0b100)
    return bits.to_bytes(9, "big")


def ws2812_frame(color: Color, order: str = "GRB") -> bytes:
    return bytes(RESET_BYTES) + encode_ws2812(color, order) + bytes(RESET_BYTES)


# ----------------------------------------------------------------------
# output backends
# ----------------------------------------------------------------------
class LedBackend(Protocol):
    def show(self, color: Color) -> None: ...

    def close(self) -> None: ...


class NullBackend:
    """Used when no LED is configured or the SPI device is unavailable."""

    def show(self, color: Color) -> None:
        return None

    def close(self) -> None:
        return None


class Ws2812Spi:
    """A single WS2812 on an SPI MOSI line via ``/dev/spidev``."""

    def __init__(self, device: str, speed_hz: int = 2_400_000, order: str = "GRB") -> None:
        self.device = device
        self.order = order
        self.fd = os.open(device, os.O_WRONLY)
        try:
            fcntl.ioctl(self.fd, _SPI_IOC_WR_MODE, (0).to_bytes(1, "little"))
            fcntl.ioctl(self.fd, _SPI_IOC_WR_BITS_PER_WORD, (8).to_bytes(1, "little"))
            fcntl.ioctl(self.fd, _SPI_IOC_WR_MAX_SPEED_HZ, speed_hz.to_bytes(4, "little"))
        except OSError:
            os.close(self.fd)
            raise

    def show(self, color: Color) -> None:
        try:
            os.write(self.fd, ws2812_frame(color, self.order))
        except OSError as exc:
            log.debug("LED write failed: %s", exc)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def open_backend(device: str, speed_hz: int, order: str) -> LedBackend:
    if not device:
        return NullBackend()
    try:
        backend = Ws2812Spi(device, speed_hz, order)
    except OSError as exc:
        log.warning("RGB LED disabled: cannot open %s (%s)", device, exc)
        return NullBackend()
    log.info("RGB LED on %s (%d Hz, %s)", device, speed_hz, order)
    return backend


# ----------------------------------------------------------------------
# animation
# ----------------------------------------------------------------------
@dataclass
class LedInstruction:
    color: Color
    fade_time: int  # ms
    length: int  # ms hold after the fade
    requeue: bool = False


class RgbLed:
    """Port of the RGB half of ``ledTask`` running as an asyncio task."""

    def __init__(self, ctx: "AppContext | None" = None, backend: LedBackend | None = None) -> None:
        self.ctx = ctx
        if backend is None:
            settings = ctx.settings if ctx is not None else None
            backend = open_backend(
                getattr(settings, "rgb_led_spi_device", ""),
                getattr(settings, "rgb_led_speed_hz", 2_400_000),
                getattr(settings, "rgb_led_color_order", "GRB"),
            )
        self.backend = backend
        self.enabled = not isinstance(backend, NullBackend)
        self.brightness = 255
        self.idle_color: Color = GREEN
        self.idle_period = 511  # ms per half cycle
        self.queue: deque[LedInstruction] = deque(maxlen=30)
        self.current: LedInstruction | None = None
        self._current_started = 0.0
        self._old_color: Color = BLACK
        self._shown: Color | None = None
        self._idle_started: float | None = None

    # ---- public API (leds.h) ------------------------------------------
    def set_brightness(self, brightness: int) -> None:
        self.brightness = max(0, min(255, int(brightness)))
        if self._shown is not None:
            self._emit(self._shown, force=True)

    def set_idle(self, color: Color, period: int | None = None) -> None:
        self.idle_color = color
        if period is not None:
            self.idle_period = max(1, period)

    def rgb_idle(self) -> None:
        """``rgbIdle``/``flushRGBQueue``: drop queued instructions, return to breathing."""
        self.queue.clear()
        if self.current is not None:
            self.current.requeue = False

    def add_fade_color(self, color: Color) -> None:
        self._enqueue(LedInstruction(color, 750, 0))

    def short_blink(self, color: Color) -> None:
        self._enqueue(LedInstruction(BLACK, 0, 3))
        self._enqueue(LedInstruction(maximize_brightness(color, 0x80), 0, 10))
        self._enqueue(LedInstruction(BLACK, 0, 3))

    def show_color_pattern(self, color_one: Color, color_two: Color, color_three: Color) -> None:
        self.queue.clear()
        colors = (BLACK, color_one, BLACK, color_two, BLACK, color_three)
        for color, length in zip(colors, PATTERN_LENGTHS):
            self._enqueue(LedInstruction(color, 0, length, requeue=True))

    def close(self) -> None:
        self.backend.close()

    # ---- internals ----------------------------------------------------
    def _enqueue(self, instruction: LedInstruction) -> None:
        if len(self.queue) < (self.queue.maxlen or 30):
            self.queue.append(instruction)

    def _emit(self, color: Color, force: bool = False) -> None:
        if color != self._shown or force:
            self._shown = color
            self.backend.show(scale(color, self.brightness))

    def _idle_step(self, now: float) -> None:
        if self._idle_started is None:
            self._idle_started = now
        elapsed_ms = int((now - self._idle_started) * 1000)
        cycle = 2 * self.idle_period
        phase = elapsed_ms % cycle
        step = phase if phase < self.idle_period else cycle - phase
        self._emit(blend(BLACK, self.idle_color, step * 255 // self.idle_period))

    def step(self, now: float | None = None) -> None:
        """One animation tick; separated from :meth:`run` for testing."""
        if now is None:
            now = time.monotonic()
        if self.current is None:
            if self.queue:
                instruction = self.queue.popleft()
                if instruction.requeue:
                    self._enqueue(LedInstruction(instruction.color, instruction.fade_time,
                                                 instruction.length, requeue=True))
                self.current = instruction
                self._current_started = now
                if instruction.fade_time <= 1:
                    self._emit(instruction.color)
            else:
                self._idle_step(now)
                return
        instruction = self.current
        elapsed_ms = int((now - self._current_started) * 1000)
        if elapsed_ms < instruction.fade_time:
            remaining = instruction.fade_time - elapsed_ms
            self._emit(blend(instruction.color, self._old_color, remaining * 255 // instruction.fade_time))
        elif elapsed_ms < instruction.fade_time + instruction.length:
            self._emit(instruction.color)
        else:
            self._emit(instruction.color)
            self._old_color = instruction.color
            self.current = None
            self._idle_started = now

    async def run(self) -> None:
        if not self.enabled:
            return
        # open with a nice RGB crossfade, like the firmware
        self._emit(BLUE)
        self._old_color = BLUE
        self.add_fade_color(RED)
        self.add_fade_color(GREEN)
        self.add_fade_color(BLUE)
        try:
            while True:
                self.step()
                await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            self._emit(BLACK, force=True)
            self.close()
            raise
