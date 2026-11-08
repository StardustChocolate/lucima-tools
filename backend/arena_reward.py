"""竞技场周奖励：固定北京时间窗口，记录本次领取的真实回执。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time

from .game_client import GameError, GameMessage
from .guild import _response_error

TASK_ID = "arena_weekly_reward"
BEIJING = timezone(timedelta(hours=8))
QUERY_ROUTE = "PVPHandler.QueryPVPData"
CLAIM_ROUTE = "PVPHandler.ChoosePVPWeekReward"
REWARDS = {"6": "黄票（神秘契约）", "2": "钻石"}
DEFAULTS = {"enabled": False, "rewardItem": "6"}
SCHEDULE_VERSION = 2
WEEK_MS = 7 * 24 * 3600_000
WINDOW_MS = 130 * 60_000
CLAIMED_FIELDS = ("IsChoosedReward", "IsChooseReward", "IsRewarded", "IsReceived")


def options(value: dict | None = None) -> dict:
    value = value if isinstance(value, dict) else {}
    return {key: value.get(key, default) for key, default in DEFAULTS.items()}


def validate(value: dict) -> dict:
    """Legacy weekday/hour/minute fields are ignored, including malformed ones."""
    cfg = options(value)
    if type(cfg["enabled"]) is not bool:
        raise ValueError("自动领取开关格式无效")
    if not isinstance(cfg["rewardItem"], str) or cfg["rewardItem"] not in REWARDS:
        raise ValueError("请选择黄票或钻石，奖励只能二选一")
    return cfg


def window(now_ms: int) -> tuple[int, int]:
    """This week's Monday window, inclusive at 05:30 and exclusive at 07:40."""
    current = datetime.fromtimestamp(now_ms / 1000, BEIJING)
    monday = (current - timedelta(days=current.weekday())).replace(
        hour=5, minute=30, second=0, microsecond=0)
    start = int(monday.timestamp() * 1000)
    return start, start + WINDOW_MS


def in_window(now_ms: int) -> bool:
    start, end = window(now_ms)
    return start <= now_ms < end


def next_slot(cfg: dict, now_ms: int, *, strictly_after: bool = False) -> int:
    start, end = window(now_ms)
    if now_ms >= end or (strictly_after and now_ms >= start):
        return start + WEEK_MS
    return start


def configure(previous: dict | None, value: dict, now_ms: int) -> dict:
    previous = previous if isinstance(previous, dict) else {}
    cfg = validate(value)
    out = {key: value for key, value in previous.items() if key not in ("weekday", "hour", "minute")}
    out.update(cfg, scheduleVersion=SCHEDULE_VERSION)
    if not cfg["enabled"]:
        out.pop("nextRunAtMs", None)
    else:
        out["nextRunAtMs"] = status(out, now_ms)["nextMs"]
    return out


def status(value: dict | None = None, now_ms: int | None = None, *, retry_at_ms: int = 0) -> dict:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    value = value if isinstance(value, dict) else {}
    try:
        cfg = validate(value)
    except ValueError as error:
        return {**DEFAULTS, "nextMs": None, "ready": False, "blocked": str(error)}
    start, end = window(now_ms)
    opened = start <= now_ms < end
    done = value.get("completedWindowStartMs") == start
    deadline = None
    retrying = False
    if cfg["enabled"]:
        deadline = start if now_ms < end and not done else start + WEEK_MS
        if opened and not done and retry_at_ms >= end:
            deadline = start + WEEK_MS
        elif opened and not done and retry_at_ms >= start:
            deadline = max(deadline, retry_at_ms)
            retrying = True
    return {**cfg, "nextMs": deadline,
            "ready": bool(cfg["enabled"] and opened and not done and now_ms >= deadline),
            "windowOpen": opened, "windowStartMs": start, "windowEndMs": end,
            "retrying": retrying, "lastReward": deepcopy(value.get("lastReward")),
            "lastCheckedAtMs": value.get("lastCheckedAtMs"), "lastResult": value.get("lastResult")}


def _find(value, key: str, depth: int = 0):
    if depth > 8:
        return None
    if isinstance(value, dict):
        if isinstance(value.get(key), dict):
            return value[key]
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _find(child, key, depth + 1)
        if found is not None:
            return found
    return None


def _merge(client, response: dict):
    for key in ("PVPData", "PVPWeekInfo"):
        found = _find(response, key)
        if found is not None:
            client.account_state[key] = deepcopy(found)


def _claimed(week: dict) -> bool:
    return any(week.get(key) is True for key in CLAIMED_FIELDS)


def _query(client, window_start_ms: int) -> dict:
    payload = client._auth_data()
    # This applies to the initial query and the post-claim confirmation query.
    # A delayed claim response must not start another query after the cutoff.
    if not window_start_ms <= int(time.time() * 1000) < window_start_ms + WINDOW_MS:
        raise GameError("竞技场周奖励窗口已结束，不再重查或重试，请自行检查领取结果")
    response = client.call(QUERY_ROUTE, payload)
    if not isinstance(response, dict):
        raise GameError("竞技场周奖励查询响应格式异常")
    if error := _response_error(response):
        raise GameError(error)
    # Only fresh query data may authorize a claim; a login snapshot can belong
    # to the preceding reward period.
    week = _find(response, "PVPWeekInfo")
    if week is None or not any(type(week.get(key)) is bool for key in CLAIMED_FIELDS):
        raise GameError("竞技场周奖励数据缺少领取状态，请提供查询响应以核对接口")
    _merge(client, response)
    return week


def _receipt_items(response: dict) -> list[dict]:
    """Drop.Items.Item is the award delta; NowItem/ItemContainer are balances.

    Only read the claim's own Drop, never login/query state or reward previews.
    Missing/invalid counts stay unknown rather than being inferred from balances.
    """
    drop = response.get("Drop")
    entries = drop.get("Items") if isinstance(drop, dict) else None
    items = {}
    for entry in entries if isinstance(entries, list) else []:
        item = entry.get("Item") if isinstance(entry, dict) else None
        if not isinstance(item, dict):
            continue
        sid = item.get("StaticID")
        if type(sid) not in (str, int) or not str(sid).strip():
            continue
        sid = str(sid)
        count = item.get("Count")
        count = count if type(count) is int and count >= 0 else None
        if sid in items:
            before = items[sid]["count"]
            count = before + count if before is not None and count is not None else None
        items[sid] = {"itemStaticId": sid, "name": REWARDS.get(sid, f"财物 {sid}"), "count": count}
    return list(items.values())


def _outside_window() -> dict:
    return {"ok": True, "skipped": True,
            "detail": "仅北京时间每周一 05:30–07:40 内领取竞技场周奖励，窗口外不补领"}


def claim(client, reward_item: str) -> dict:
    if reward_item not in REWARDS:
        raise GameError("请选择黄票或钻石")
    now_ms = int(time.time() * 1000)
    start, end = window(now_ms)
    if not start <= now_ms < end:
        return _outside_window()
    if _claimed(_query(client, start)):
        return {"ok": True, "alreadyClaimed": True, "detail": "竞技场本期周奖励已领取"}
    # Querying (or waiting for another task/account lock) can cross the cutoff.
    # Recheck immediately before the mutating game request, including retries.
    payload = {**client._auth_data(), "ItemStaticID": reward_item}
    if not start <= int(time.time() * 1000) < end:
        return _outside_window()
    try:
        response = client.call(CLAIM_ROUTE, payload)
        if isinstance(response, dict) and (error := _response_error(response)):
            raise GameMessage(error)
    except GameMessage as error:
        message = error.msg.casefold()
        # Exact arena-specific hints only: broad '已/Already/Reward' matching
        # could turn a failed request into a completed weekly schedule.
        if any(hint in message for hint in ("isrewarded", "already rewarded", "alreadyreward", "choosedreward")):
            return {"ok": True, "alreadyClaimed": True, "detail": "竞技场本期周奖励已领取"}
        raise
    if not isinstance(response, dict):
        raise GameError("竞技场奖励领取响应格式异常")
    _merge(client, response)
    receipt = _find(response, "PVPWeekInfo")
    if not (receipt is not None and _claimed(receipt)) and not _claimed(_query(client, start)):
        raise GameError("竞技场奖励领取结果未确认，将重新查询后重试")
    receipt = {"claimedAtMs": int(time.time() * 1000), "requestedItem": reward_item,
               "items": _receipt_items(response)}
    details = "、".join(f"{item['name']} ×{item['count']}" if item['count'] is not None
                       else f"{item['name']}（未返回数量）" for item in receipt["items"])
    if not details:
        details = f"未返回奖励财物明细，未返回数量（本次选择：{REWARDS[reward_item]}）"
    return {"ok": True, "lastReward": receipt, "detail": f"竞技场周奖励领取成功：{details}"}


def run(client, params: dict | None = None) -> dict:
    params = params if isinstance(params, dict) else {}
    now_ms = int(time.time() * 1000)
    current = status(params, now_ms)
    if current.get("blocked"):
        return {"ok": False, "detail": current["blocked"]}
    if not current["enabled"] or not current["ready"]:
        return {"ok": True, "skipped": True, "detail": "竞技场奖励未开启或不在本周可领取窗口"}
    result = claim(client, current["rewardItem"])
    finished_ms = int(time.time() * 1000)
    if not result.get("skipped"):
        params.update(completedWindowStartMs=current["windowStartMs"],
                      lastCheckedAtMs=finished_ms, lastResult=result["detail"])
        if not result.get("alreadyClaimed"):
            params.update(lastReward=deepcopy(result["lastReward"]), lastCompletedAtMs=finished_ms)
    params["nextRunAtMs"] = status(params, finished_ms)["nextMs"]
    return result
