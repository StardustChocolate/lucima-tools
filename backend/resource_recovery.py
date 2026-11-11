"""Read-only natural recovery, anchored to confirmed item counts and server time."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from . import game_clock, runtime_data as resources

RESOURCE_IDS = {"stamina": "3", "flag": "4"}


def build_intervals(items):
    result = {}
    for row in items:
        if row.get("ID") not in RESOURCE_IDS.values():
            continue
        for field in ("DynamicField1", "DynamicField2"):
            key, _, value = (row.get(field) or "").partition("#")
            if key != "ItemCollectSecond":
                continue
            seconds = int(value)
            if seconds <= 0:
                raise ValueError(f"物品 {row['ID']} 的自然恢复间隔无效")
            result[row["ID"]] = seconds
    return result


@resources.cached
def intervals():
    value = resources.read_json("assets/game_config.json", {}).get("itemRecoverySeconds")
    if value is None:
        # Previously compiled downloads do not yet include this index.
        root = Path(os.environ.get("ARK_ASSET_ROOT") or Path(__file__).resolve().parents[1])
        try:
            value = json.loads((root / "assets/game_config.json").read_text(encoding="utf-8")).get("itemRecoverySeconds")
        except (OSError, ValueError):
            value = None
    return value if isinstance(value, dict) else {}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def gauge(item, now_ms, seconds, *, connected=True, grace_ms=0):
    timer = item.get("TimeCollect")
    timer = timer if isinstance(timer, dict) else {}
    count, maximum = item.get("Count", 0), timer.get("MaxCount")
    result = {"current": count, "max": maximum}
    last_ms = timer.get("LastCollectTime")
    if isinstance(last_ms, dict):
        last_ms = last_ms.get("$date")
    if not (number(count) and number(maximum) and 0 <= count < maximum
            and number(last_ms) and last_ms > 0 and number(seconds) and seconds > 0):
        return result
    interval_ms = int(seconds * 1000)
    if interval_ms <= 0:
        return result
    last_ms += grace_ms
    steps = max(0, int((now_ms - last_ms) // interval_ms))
    result["current"] = min(maximum, count + steps)
    if connected and result["current"] < maximum:
        result["recovery"] = {"intervalMs": interval_ms,
                              "nextInMs": last_ms + (steps + 1) * interval_ms - now_ms}
    return result


def snapshot(items, now_ms, *, connected=True):
    rules = intervals()
    result = {key: gauge(items.get(sid, {}), now_ms, rules.get(sid), connected=connected)
              for key, sid in RESOURCE_IDS.items()}
    # Confirmed flags can be spent immediately; inferred recoveries need a margin.
    result["flag"]["spendable"] = gauge(items.get("4", {}), now_ms, rules.get("4"),
        connected=connected, grace_ms=game_clock.TASK_BOUNDARY_GRACE_MS)
    return result
