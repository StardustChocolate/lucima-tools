"""Per-account game time, sampled from responses without extra requests."""
from __future__ import annotations

import math
import threading
import time


# Automatic actions wait beyond game-side eligibility timestamps.
TASK_BOUNDARY_GRACE_MS = 5_000


class ServerClock:
    def __init__(self):
        self._lock = threading.Lock()
        self._sample = None

    def sync(self, response, *, reset=False):
        value = response.get("Utc") if isinstance(response, dict) else None
        try:
            stamp = float(value) * 1000 if not isinstance(value, bool) else math.nan
            stamp = int(stamp) if math.isfinite(stamp) and stamp > 0 else None
        except (TypeError, ValueError, OverflowError):
            stamp = None
        local_ms = int(time.time() * 1000)
        with self._lock:
            if stamp is not None:
                if self._sample is not None and not reset:
                    previous, sampled_at = self._sample
                    stamp = max(stamp, previous + max(0, local_ms - sampled_at))
                self._sample = (stamp, local_ms)
            elif reset:
                self._sample = None

    def now_ms(self):
        with self._lock:
            sample = self._sample
        if sample is None:
            return int(time.time() * 1000)
        stamp, sampled_at = sample
        return stamp + max(0, int(time.time() * 1000) - sampled_at)


_init_lock = threading.Lock()


def _clock(client):
    # Also supports lightweight clients and in-memory sessions created before
    # the clock was introduced. Production sessions initialize at login receipt.
    with _init_lock:
        clock = getattr(client, "_game_clock", None)
        state = getattr(client, "account_state", None)
        state_utc = state.get("Utc") if isinstance(state, dict) else None
        if clock is None:
            clock = client._game_clock = ServerClock()
        if (getattr(client, "_game_clock_state", None) is not state
                or getattr(client, "_game_clock_state_utc", object()) != state_utc):
            clock.sync(state, reset=True)
            client._game_clock_state = state
            client._game_clock_state_utc = state_utc
        return clock


def sync(client, response, *, reset=False):
    clock = _clock(client)
    clock.sync(response, reset=reset)
    state = getattr(client, "account_state", None)
    if response is state:
        client._game_clock_state_utc = state.get("Utc") if isinstance(state, dict) else None


def now_ms(client):
    return _clock(client).now_ms()


def local_deadline(client, deadline_ms, *, local_now_ms=None, server_now_ms=None):
    """Translate a game timestamp for Date.now() and the local scheduler."""
    if deadline_ms is None:
        return None
    local_now_ms = int(time.time() * 1000) if local_now_ms is None else int(local_now_ms)
    server_now_ms = now_ms(client) if server_now_ms is None else int(server_now_ms)
    return int(deadline_ms) + local_now_ms - server_now_ms
