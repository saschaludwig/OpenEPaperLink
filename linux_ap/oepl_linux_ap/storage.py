"""Helpers that map LittleFS style paths (``/current/x.raw``) onto the data directory."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


class ContentFS:
    """Thin wrapper over the data directory with the ESP path conventions."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def path(self, name: str | os.PathLike) -> Path:
        text = str(name)
        rel = text.lstrip("/")
        if ".." in Path(rel).parts:
            raise ValueError(f"invalid path {text}")
        return self.settings.data_path / rel

    def exists(self, name: str) -> bool:
        try:
            return self.path(name).exists()
        except ValueError:
            return False

    def size(self, name: str) -> int:
        try:
            return self.path(name).stat().st_size
        except (OSError, ValueError):
            return 0

    def read_bytes(self, name: str) -> bytes | None:
        try:
            return self.path(name).read_bytes()
        except (OSError, ValueError) as exc:
            log.debug("read %s failed: %s", name, exc)
            return None

    def write_bytes(self, name: str, data: bytes) -> bool:
        try:
            target = self.path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return True
        except (OSError, ValueError) as exc:
            log.error("write %s failed: %s", name, exc)
            return False

    def remove(self, name: str) -> bool:
        try:
            self.path(name).unlink()
            return True
        except (OSError, ValueError):
            return False

    def rename(self, src: str, dst: str) -> bool:
        try:
            target = self.path(dst)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.path(src), target)
            return True
        except (OSError, ValueError) as exc:
            log.error("rename %s -> %s failed: %s", src, dst, exc)
            return False

    def md5(self, name: str) -> bytes | None:
        data = self.read_bytes(name)
        if data is None:
            return None
        return hashlib.md5(data).digest()

    @staticmethod
    def normalise(name: str) -> str:
        return name if name.startswith("/") else "/" + name
