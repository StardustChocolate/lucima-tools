"""Pure source-page parsing shared by the server collector and offline checks."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

SOURCE_URL = "https://ucngame.com/codes/ark-recode-redeem-codes/"
GAME_TZ = timezone(timedelta(hours=8))
_CODE_RE = re.compile(r"\bArk[A-Za-z0-9]{6,}\b")
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_VALID_UNTIL_RE = re.compile(
    r"\bvalid\s+until\b\s*:?\s*(" + "|".join(_MONTHS) + r")"
    r"\s+(\d{1,2})(?:\s*(?:st|nd|rd|th))?(?:\s*,\s*|\s+)(\d{4})\b",
    re.IGNORECASE,
)
with Path(__file__).with_name("redeem_code_exclusions.json").open(encoding="utf-8") as stream:
    _EXCLUDED_CODES = frozenset(json.load(stream)["codes"])


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None
        elif self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _parse_valid_until(text: str) -> str | None:
    # Added-on dates and dates without an explicit year are not expiry dates.
    match = _VALID_UNTIL_RE.search(text or "")
    if not match:
        return None
    month = _MONTHS.index(match.group(1).lower()) + 1
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_codes(document: str, today: date | None = None, *, require_codes: bool = False) -> list[dict]:
    parser = _TableParser()
    parser.feed(document or "")
    today = today or datetime.now(GAME_TZ).date()
    found: dict[str, dict] = {}
    recognized = False
    for row in parser.rows:
        text = " ".join(row)
        match = _CODE_RE.search(text)
        if not match:
            continue
        recognized = True
        code = match.group(0)
        if code in _EXCLUDED_CODES:
            continue
        valid_until = _parse_valid_until(text)
        if valid_until and valid_until < today.isoformat():
            continue
        found[code] = {
            "code": code,
            "validUntil": valid_until,
            "source": "ucngame",
        }
    if require_codes and not recognized:
        raise RuntimeError("网页未解析到兑换码")
    return list(found.values())
