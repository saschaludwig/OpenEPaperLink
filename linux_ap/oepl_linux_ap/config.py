"""Runtime configuration for the Linux access point.

The settings here describe the *machine* (serial ports, GPIO lines, data
directory, listen address).  Everything the user can change from the web
interface lives in ``apconfig.json`` inside the data directory and is handled
by :mod:`oepl_linux_ap.tagdb`.

Settings are loaded from a JSON file (``--config``), then overridden by
environment variables prefixed with ``OEPL_`` (for example
``OEPL_SERIAL_AP_PORT=/dev/ttyUSB0``).
"""

from __future__ import annotations

import json
import os
import typing
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# Repository layout relative to this file: linux_ap/oepl_linux_ap/config.py
PACKAGE_DIR = Path(__file__).resolve().parent
LINUX_AP_DIR = PACKAGE_DIR.parent
REPO_DIR = LINUX_AP_DIR.parent

DEFAULT_WWWROOT = REPO_DIR / "ESP32_AP-Flasher" / "wwwroot"
DEFAULT_FONTS = REPO_DIR / "ESP32_AP-Flasher" / "data" / "fonts"
DEFAULT_TAGTYPES = REPO_DIR / "resources" / "tagtypes"
DEFAULT_LANGUAGES = REPO_DIR / "ESP32_AP-Flasher" / "data" / "languages.json"
DEFAULT_UPDATE_ACTIONS = REPO_DIR / "ESP32_AP-Flasher" / "data" / "update_actions.json"
DEFAULT_C6_BINARIES = REPO_DIR / "binaries" / "ESP32-C6"


@dataclass
class Settings:
    """Machine level settings of the access point service."""

    # HTTP server
    http_host: str = "0.0.0.0"
    http_port: int = 80

    # UART connected to the C6 "AP" UART (C6 GPIO2/GPIO3)
    serial_ap_port: str = "/dev/ttyS1"
    # UART connected to the C6 UART0 (GPIO16/GPIO17) used by the ROM bootloader
    serial_debug_port: str = "/dev/ttyS2"
    # Switch the AP link to 2 Mbaud after the handshake (HSPD command)
    serial_ap_high_speed: bool = True

    # GPIO lines used to reset and to put the C6 into download mode.
    # Both are active low on the C6 side (EN and GPIO9/BOOT).
    gpio_chip: str = "/dev/gpiochip0"
    gpio_reset_line: int = 203
    gpio_boot_line: int = 6

    # Persistent storage root.  Mirrors the LittleFS root of the ESP firmware:
    # <data_dir>/current, <data_dir>/tagtypes, <data_dir>/current/apconfig.json ...
    data_dir: str = "/var/lib/oepl"

    # Static assets.  They default to the files inside this repository so the
    # service can run straight from a checkout.
    wwwroot_dir: str = str(DEFAULT_WWWROOT)
    fonts_dir: str = str(DEFAULT_FONTS)
    tagtypes_seed_dir: str = str(DEFAULT_TAGTYPES)
    languages_file: str = str(DEFAULT_LANGUAGES)
    update_actions_file: str = str(DEFAULT_UPDATE_ACTIONS)
    c6_binaries_dir: str = str(DEFAULT_C6_BINARIES)

    # Behaviour toggles
    enable_udp_sync: bool = True
    udp_multicast: bool = True
    log_level: str = "INFO"

    # Free form, for future use
    extra: dict[str, Any] = field(default_factory=dict)

    # ---- derived paths -------------------------------------------------
    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def current_dir(self) -> Path:
        return self.data_path / "current"

    @property
    def tagtypes_dir(self) -> Path:
        return self.data_path / "tagtypes"

    @property
    def apconfig_file(self) -> Path:
        return self.current_dir / "apconfig.json"

    @property
    def tagdb_file(self) -> Path:
        return self.current_dir / "tagDB.json"

    @property
    def log_file(self) -> Path:
        return self.data_path / "log.txt"

    @property
    def www_path(self) -> Path:
        """Directory served as the web root.

        A ``www`` folder inside the data directory wins over the repository
        copy so that a user can upload a newer web UI via ``littlefs_put``
        exactly like on the ESP.
        """
        override = self.data_path / "www"
        if (override / "index.html").exists():
            return override
        return Path(self.wwwroot_dir)

    def font_search_paths(self) -> list[Path]:
        return [self.data_path / "fonts", Path(self.fonts_dir)]


def _coerce(value: str, target_type: Any) -> Any:
    """Convert an environment string into the dataclass field type."""
    if target_type is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(value, 0)
    return value


def load_settings(config_file: str | os.PathLike | None = None) -> Settings:
    """Build :class:`Settings` from an optional JSON file and the environment."""
    values: dict[str, Any] = {}
    if config_file:
        path = Path(config_file)
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                values.update(json.load(handle))

    settings = Settings(**{k: v for k, v in values.items() if k in {f.name for f in fields(Settings)}})
    settings.extra.update({k: v for k, v in values.items() if k not in {f.name for f in fields(Settings)}})

    type_hints = typing.get_type_hints(Settings)
    for item in fields(Settings):
        env_name = "OEPL_" + item.name.upper()
        if env_name in os.environ:
            setattr(settings, item.name, _coerce(os.environ[env_name], type_hints.get(item.name, str)))
    return settings
