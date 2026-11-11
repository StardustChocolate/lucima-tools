"""Shared hourly source-page cache; game redemption records stay per account."""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone

import httpx

from . import config
from .redeem_code_parser import SOURCE_URL, GAME_TZ, _EXCLUDED_CODES, parse_codes

REFRESH_SECONDS = 60 * 60


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


_lock = threading.RLock()
_cache = config.load_redeem_codes()
_revision = 1 if _cache.get("codes") else 0
_failures = int(_cache.get("failures") or 0)


def _restore_schedule(cache: dict, source_url: str, now: float) -> float:
    # Switching away from the API retries immediately; subsequent restarts
    # preserve the webpage's hourly deadline, including after failures.
    if (cache.get("fetchMode") != "webpage" or cache.get("sourceUrl") != source_url
            or cache.get("refreshIntervalSeconds") != REFRESH_SECONDS):
        return 0.0
    try:
        saved = datetime.fromisoformat(str(cache["nextRefreshAt"])).timestamp()
        if not math.isfinite(saved):
            return 0.0
        return min(saved, now + REFRESH_SECONDS)
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0.0


_next_refresh_at = _restore_schedule(_cache, SOURCE_URL, time.time())
_last_error = _cache.get("refreshError")

_scheduler_lock = threading.Lock()
_scheduler_started = False
_scheduler_stop = threading.Event()


def _refresh_loop() -> None:
    while not _scheduler_stop.wait(min(next_refresh_delay(), 300.0)):
        refresh_if_due()


def start_scheduler() -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
        threading.Thread(target=_refresh_loop, name="redeem-code-refresh", daemon=True).start()


def _state_locked() -> dict:
    return {
        "source": SOURCE_URL,
        "sourceUrl": SOURCE_URL,
        "sourceError": None,
        "lastSynced": _cache.get("fetchedAt"),
        "lastUpdated": _cache.get("scrapedAt"),
        "nextRefreshAt": _iso(_next_refresh_at),
        "refreshError": _last_error,
        "codes": [dict(code) for code in (_cache.get("codes") or [])
                  if code.get("code") not in _EXCLUDED_CODES],
        "revision": _revision,
    }


def normalize_record(record: dict | None) -> dict:
    normalized = dict(record or {})
    status = normalized.get("status")
    if status in {"redeemed", "already_redeemed"}:
        normalized["status"] = "redeemed"
        normalized.pop("message", None)
        normalized.pop("errorReason", None)
    elif status == "expired":
        normalized["status"] = "expired"
    elif status == "invalid" or normalized.get("errorReason") == "invalid":
        normalized["status"] = "invalid"
        normalized["errorReason"] = "invalid"
    else:
        normalized["status"] = "pending"
        if status == "failed":
            normalized.setdefault("errorReason", "transient")
    return normalized


def get_state(records: dict | None = None) -> dict:
    with _lock:
        state = _state_locked()
    for entry in state["codes"]:
        entry.pop("content", None)
        record = normalize_record((records or {}).get(entry["code"]))
        entry.update({key: record[key] for key in ("status", "detail", "message") if key in record})
    return state


def revision() -> int:
    with _lock:
        return _revision


def next_refresh_delay() -> float:
    with _lock:
        return max(0.0, _next_refresh_at - time.time())


def _fetch() -> str:
    proxy = config.get_proxy()
    with httpx.Client(proxy=proxy or None, timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True) as client:
        response = client.get(SOURCE_URL, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html",
            "Referer": "https://www.google.com/",
            "Cache-Control": "no-cache",
        })
        response.raise_for_status()
        return response.text


def _wake_accounts() -> None:
    try:
        from . import accounts
        for account in accounts.all_accounts():
            account._wake.set()
    except Exception:
        pass


def refresh_if_due(now: float | None = None, *, initial: bool = False) -> dict:
    """All callers share one hourly deadline, including startup and failures."""
    global _failures, _last_error, _next_refresh_at, _revision, _cache
    requested_at = time.time() if now is None else now
    changed = False
    with _lock:
        if requested_at < _next_refresh_at:
            return _state_locked()
        try:
            document = _fetch()
            completed_at = time.time() if now is None else now
            codes = parse_codes(document, today=datetime.fromtimestamp(completed_at, GAME_TZ).date(),
                                require_codes=True)
            next_at = completed_at + REFRESH_SECONDS
            new_cache = {
                "codes": codes,
                "scrapedAt": _iso(completed_at),
                "sourceUrl": SOURCE_URL,
                "fetchMode": "webpage",
                "refreshIntervalSeconds": REFRESH_SECONDS,
                "fetchedAt": _iso(completed_at),
                "nextRefreshAt": _iso(next_at),
                "refreshError": None,
                "failures": 0,
            }
            config.save_redeem_codes(new_cache)
            changed = codes != _cache.get("codes", [])
            _cache = new_cache
            _failures = 0
            _last_error = None
            _next_refresh_at = next_at
            if changed:
                _revision += 1
            state = _state_locked()
        except Exception as error:
            _failures += 1
            _last_error = str(error)
            completed_at = time.time() if now is None else now
            _next_refresh_at = completed_at + REFRESH_SECONDS
            _cache = {key: value for key, value in _cache.items() if key not in {"apiUrl", "sourceError"}}
            _cache = {**_cache, "sourceUrl": SOURCE_URL, "fetchMode": "webpage",
                      "refreshIntervalSeconds": REFRESH_SECONDS,
                      "refreshError": _last_error, "failures": _failures,
                      "nextRefreshAt": _iso(_next_refresh_at)}
            try:
                config.save_redeem_codes(_cache)
            except Exception:
                pass
            return _state_locked()
    if changed:
        _wake_accounts()
    return state
