"""Minimal GPIO output abstraction for the C6 reset and boot lines.

Backends, tried in order:

1. ``gpiod`` v2 API (python3-libgpiod >= 2.0, Debian bookworm / DietPi 9)
2. ``gpiod`` v1 API (python3-libgpiod 1.x)
3. the legacy sysfs interface (``/sys/class/gpio``)
4. a no-op backend that only logs, so development on a PC works

Both C6 lines are active low: EN resets while low, GPIO9 selects download
mode when low during reset.  The service keeps them high when idle.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class OutputLine:
    """One output line that can be driven high or low."""

    def __init__(self, chip: str, offset: int, name: str, initial: int = 1):
        self.chip = chip
        self.offset = offset
        self.name = name
        self._backend = "none"
        self._request = None
        self._line = None
        self._sysfs: Path | None = None
        self._open(initial)

    # -- backends -----------------------------------------------------------
    def _open(self, initial: int) -> None:
        if self.offset < 0:
            log.info("GPIO %s disabled (offset < 0)", self.name)
            return
        try:
            import gpiod  # type: ignore

            if hasattr(gpiod, "request_lines"):
                from gpiod.line import Direction, Value  # type: ignore

                value = Value.ACTIVE if initial else Value.INACTIVE
                self._request = gpiod.request_lines(
                    self.chip,
                    consumer="oepl-linux-ap",
                    config={self.offset: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=value)},
                )
                self._backend = "gpiod2"
                return
            chip = gpiod.Chip(self.chip)
            line = chip.get_line(self.offset)
            line.request(consumer="oepl-linux-ap", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[initial])
            self._line = line
            self._backend = "gpiod1"
            return
        except ImportError:
            pass
        except Exception as exc:  # pragma: no cover - hardware specific
            log.warning("gpiod failed for %s (line %d): %s", self.name, self.offset, exc)

        sysfs_root = Path("/sys/class/gpio")
        if sysfs_root.exists():
            try:
                target = sysfs_root / f"gpio{self.offset}"
                if not target.exists():
                    (sysfs_root / "export").write_text(str(self.offset))
                    time.sleep(0.1)
                (target / "direction").write_text("high" if initial else "low")
                self._sysfs = target
                self._backend = "sysfs"
                return
            except OSError as exc:  # pragma: no cover - hardware specific
                log.warning("sysfs GPIO failed for %s: %s", self.name, exc)
        log.warning("no GPIO backend for %s; reset/boot control is a no-op", self.name)

    # -- API -----------------------------------------------------------------
    def set(self, value: int) -> None:
        value = 1 if value else 0
        if self._backend == "gpiod2":
            from gpiod.line import Value  # type: ignore

            self._request.set_value(self.offset, Value.ACTIVE if value else Value.INACTIVE)
        elif self._backend == "gpiod1":
            self._line.set_value(value)
        elif self._backend == "sysfs" and self._sysfs is not None:
            (self._sysfs / "value").write_text(str(value))
        else:
            log.debug("GPIO %s -> %d (no-op)", self.name, value)

    def high(self) -> None:
        self.set(1)

    def low(self) -> None:
        self.set(0)

    def close(self) -> None:
        try:
            if self._backend == "gpiod2" and self._request is not None:
                self._request.release()
            elif self._backend == "gpiod1" and self._line is not None:
                self._line.release()
        except Exception:  # pragma: no cover
            pass
        self._request = None
        self._line = None

    @property
    def available(self) -> bool:
        return self._backend != "none"


class C6Control:
    """Reset and boot control of the ESP32-C6 (``APTagReset`` and the flasher)."""

    def __init__(self, chip: str, reset_offset: int, boot_offset: int):
        self.reset = OutputLine(chip, reset_offset, "C6_EN")
        self.boot = OutputLine(chip, boot_offset, "C6_BOOT")

    def hard_reset(self) -> None:
        """Mirror ``APTagReset``: boot high, reset low 50 ms, wait, release."""
        self.boot.high()
        self.reset.low()
        time.sleep(0.05)
        # The Yellow AP has no soft power switch; the firmware waits 300 ms here.
        time.sleep(0.3)
        time.sleep(0.1)
        self.reset.high()
        time.sleep(0.1)

    def enter_bootloader(self) -> None:
        """Hold BOOT low across a reset pulse so the ROM loader starts."""
        self.boot.low()
        time.sleep(0.01)
        self.reset.low()
        time.sleep(0.1)
        self.reset.high()
        time.sleep(0.05)
        self.boot.high()

    def close(self) -> None:
        self.reset.close()
        self.boot.close()

    @property
    def available(self) -> bool:
        return self.reset.available
