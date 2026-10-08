"""Translations of weekday and month names (``language.cpp``).

``languages.json`` maps the numeric ``language`` setting onto day/month names
and the two ``strftime`` date formats used by the content types.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT = {
    "language": "en",
    "daysShort": ["SU", "MO", "TU", "WE", "TH", "FR", "SA"],
    "days": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
    "months": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"],
    "date_format": ["%m/%d/%Y", "%m/%d"],
}


class Language:
    def __init__(self, languages_file: Path, language_id: int = 0):
        self._file = languages_file
        self._table: dict[str, dict] = {}
        self.language_id = language_id
        self.reload(language_id)

    def reload(self, language_id: int) -> None:
        self.language_id = language_id
        try:
            self._table = json.loads(Path(self._file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("languages.json not readable (%s), using English", exc)
            self._table = {}

    @property
    def _entry(self) -> dict:
        return self._table.get(str(self.language_id), _DEFAULT)

    @property
    def language_code(self) -> str:
        return self._entry.get("language", "en")

    @property
    def days_short(self) -> list[str]:
        return self._entry.get("daysShort", _DEFAULT["daysShort"])

    @property
    def days(self) -> list[str]:
        return self._entry.get("days", _DEFAULT["days"])

    @property
    def months(self) -> list[str]:
        return self._entry.get("months", _DEFAULT["months"])

    @property
    def date_format_long(self) -> str:
        return self._entry.get("date_format", _DEFAULT["date_format"])[0]

    @property
    def date_format_short(self) -> str:
        fmt = self._entry.get("date_format", _DEFAULT["date_format"])
        return fmt[1] if len(fmt) > 1 else fmt[0]
