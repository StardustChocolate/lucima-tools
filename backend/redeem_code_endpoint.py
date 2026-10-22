"""Read the code API address without starting application update checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit


def _url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        parsed = urlsplit(value)
        parsed.port
        if parsed.scheme in {"https", "http"} and parsed.hostname \
                and not parsed.username and not parsed.password \
                and len(value) <= 2000 and not any(ord(c) < 32 for c in value):
            return value
    except ValueError:
        pass
    return ""


def _paths(filename: str):
    root = os.environ.get("ARK_ASSET_ROOT", "").strip()
    if root:
        yield Path(root) / filename
    yield Path(__file__).resolve().parent / filename
    yield Path(__file__).resolve().parent.parent / filename


def _read_url(filename: str, field: str) -> str:
    for path in _paths(filename):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = _url(data.get(field)) if isinstance(data, dict) else ""
        except (OSError, ValueError):
            continue
        if value:
            return value
    return ""


def api_url() -> str:
    """Dedicated configuration takes priority over the existing update host."""
    explicit = _url(os.environ.get("LUCIMA_REDEEM_CODES_URL", "")) \
        or _read_url("redeem-codes-endpoint.json", "apiUrl")
    if explicit:
        return explicit
    update = _url(os.environ.get("LUCIMA_UPDATE_URL", "")) \
        or _read_url("update-endpoint.json", "manifestUrl")
    return urljoin(update, "/api/redeem-codes") if update else ""
