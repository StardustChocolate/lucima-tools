"""Shared hourly code API cache; game redemption records stay per account."""
from __future__ import annotations

import math
import threading
import time
from datetime import date, datetime, timezone

import httpx

from . import config
from .redeem_code_endpoint import api_url
from .redeem_code_parser import SOURCE_URL, GAME_TZ, _CODE_RE, _EXCLUDED_CODES, parse_codes

REFRESH_SECONDS = 60 * 60


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


_lock = threading.RLock()
_cache = config.load_redeem_codes()
_revision = 1 if _cache.get("codes") else 0
_failures = int(_cache.get("failures") or 0)


def _restore_schedule(cache: dict, endpoint: str, now: float) -> float:
    # Old webpage caches migrate immediately; a restart preserves API cadence.
    if cache.get("apiUrl") != endpoint or cache.get("refreshIntervalSeconds") != REFRESH_SECONDS:
        return 0.0
    try:
        saved = datetime.fromisoformat(str(cache["nextRefreshAt"])).timestamp()
        if not math.isfinite(saved):
            return 0.0
        return min(saved, now + REFRESH_SECONDS)
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0.0


_next_refresh_at = _restore_schedule(_cache, api_url(), time.time())
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
        "source": api_url(),
        "sourceUrl": SOURCE_URL,
        "sourceError": _cache.get("sourceError"),
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


def _timestamp(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("兑换码 API 的更新时间格式错误")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("兑换码 API 的更新时间缺少时区")
    return parsed.astimezone(timezone.utc).isoformat()


def _validate_api(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise ValueError("兑换码 API 的数据版本无效")
    rows = payload.get("codes")
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("兑换码 API 的兑换码表无效")
    updated = _timestamp(payload.get("updatedAt"), optional=True)
    if not updated:
        raise ValueError("服务器尚未成功抓取兑换码：" + str(payload.get("refreshError") or "等待更新"))
    codes = {}
    for entry in rows:
        if not isinstance(entry, dict):
            raise ValueError("兑换码 API 含无效条目")
        code = entry.get("code")
        if not isinstance(code, str) or len(code) > 128 or not _CODE_RE.fullmatch(code):
            raise ValueError("兑换码 API 含无效兑换码")
        expiry = entry.get("validUntil")
        if expiry is not None and (not isinstance(expiry, str) or date.fromisoformat(expiry).isoformat() != expiry):
            raise ValueError("兑换码 API 含无效有效期")
        if code not in _EXCLUDED_CODES:
            codes[code] = {"code": code, "validUntil": expiry, "source": "ucngame"}
    error = payload.get("refreshError")
    if error is not None and not isinstance(error, str):
        raise ValueError("兑换码 API 的更新状态无效")
    return {"codes": list(codes.values()), "scrapedAt": updated, "sourceError": error}


def _fetch() -> dict:
    endpoint = api_url()
    if not endpoint:
        raise RuntimeError("未配置兑换码服务地址")
    proxy = config.get_proxy()
    with httpx.Client(proxy=proxy or None, timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True) as client:
        response = client.get(endpoint, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
        response.raise_for_status()
        return response.json()


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
        endpoint = api_url()
        try:
            result = _validate_api(_fetch())
            completed_at = time.time() if now is None else now
            next_at = completed_at + REFRESH_SECONDS
            new_cache = {
                **result,
                "apiUrl": endpoint,
                "refreshIntervalSeconds": REFRESH_SECONDS,
                "fetchedAt": _iso(completed_at),
                "nextRefreshAt": _iso(next_at),
                "refreshError": None,
                "failures": 0,
            }
            config.save_redeem_codes(new_cache)
            changed = result["codes"] != _cache.get("codes", [])
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
            _cache = {**_cache, "apiUrl": endpoint, "refreshIntervalSeconds": REFRESH_SECONDS,
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
