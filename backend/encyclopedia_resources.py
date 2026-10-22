"""Local configuration for encyclopedia media, independent of app updates.

The browser downloads the resource manifest and images on demand. Reading this
configuration never contacts the update server or starts an update check.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit


def _http_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url or len(url) > 2000 or any(ord(char) < 32 for char in url):
        return ""
    try:
        parsed = urlsplit(url)
        # Accessing port also validates malformed/non-numeric port values.
        parsed.port
        if parsed.scheme not in ("http", "https") or not parsed.hostname \
                or parsed.username or parsed.password:
            return ""
    except ValueError:
        return ""
    return url


def _config_paths(filename: str) -> list[Path]:
    paths = []
    asset_root = os.environ.get("ARK_ASSET_ROOT", "").strip()
    if asset_root:
        paths.append(Path(asset_root) / filename)
    paths.extend((
        Path(__file__).resolve().parent / filename,
        Path(__file__).resolve().parent.parent / filename,
    ))
    return paths


def _read_endpoint(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _resource_endpoint(value: dict) -> dict[str, str]:
    manifest_url = _http_url(value.get("manifestUrl"))
    base_url = _http_url(value.get("baseUrl"))
    if not manifest_url and not base_url:
        return {}
    if not manifest_url:
        manifest_url = urljoin(base_url.rstrip("/") + "/", "manifest.json")
    if not base_url:
        base_url = urljoin(manifest_url, ".")
    return {"manifestUrl": manifest_url, "baseUrl": base_url}


def resource_config() -> dict[str, str]:
    """Return media endpoints without relying on UpdateService or its status.

    Explicit media configuration takes priority. Packaged builds can reuse the
    host of their existing update endpoint, while start.bat can use a separate
    encyclopedia-endpoint.json without enabling software update checks.
    """
    configured = os.environ.get("LUCIMA_ENCYCLOPEDIA_URL", "").strip()
    if configured:
        return _resource_endpoint({"manifestUrl": configured})
    for path in _config_paths("encyclopedia-endpoint.json"):
        endpoint = _resource_endpoint(_read_endpoint(path))
        if endpoint:
            return endpoint

    update_url = _http_url(os.environ.get("LUCIMA_UPDATE_URL", ""))
    if not update_url:
        for path in _config_paths("update-endpoint.json"):
            update_url = _http_url(_read_endpoint(path).get("manifestUrl"))
            if update_url:
                break
    if not update_url:
        return {}
    return _resource_endpoint({
        "manifestUrl": urljoin(update_url, "/encyclopedia/manifest.json"),
    })
