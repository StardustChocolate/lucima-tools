"""Captain-shop automatic purchases backed by account and guild snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
import time

from .game_client import GameError
from .guild import (CAPTAIN_SHOP_AUTHORITIES, CAPTAIN_SHOP_PERMISSION_MESSAGE,
                    has_guild, _nonnegative_int)
from .guild_store import record_stamp

UTC = timezone.utc
BUFF_REFRESH_MS = 11 * 60 * 60 * 1000 + 54 * 60 * 1000
CAPTAIN_GOODS = {
    "GuildMoneyBonus": {"name": "富豪的祝福", "currency": "80", "price": 200000, "kind": "buff"},
    "GuildHeroExp": {"name": "知识的祝福", "currency": "80", "price": 200000, "kind": "buff"},
    "GuildEnergy30": {"name": "30 个能源", "currency": "80", "price": 1000000, "kind": "limited"},
    "GuildEnergy50": {"name": "50 个能源", "currency": "81", "price": 30, "kind": "limited"},
    "GuildBox": {"name": "佣兵团补给箱", "currency": "81", "price": 300, "kind": "limited"},
}


@lru_cache(maxsize=1)
def _catalog():
    # The catalog is packaged with the backend Python module.  On Android,
    # ARK_ASSET_ROOT points at web/ and therefore does not contain backend/.
    path = Path(__file__).resolve().with_name("commodity_catalog.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("commodities"), dict):
        raise GameError("团长商店清单无效，请更新应用资源")
    goods = {}
    for identifier, display in CAPTAIN_GOODS.items():
        row = data["commodities"].get(identifier)
        if not isinstance(row, dict) or row.get("store") != "CaptainShop":
            raise GameError("团长商店商品清单不完整，请更新应用资源")
        goods[identifier] = {**display, "limit": row.get("limit"), "reset": row.get("resetType")}
    return goods


def captain_store_options(value=None):
    value = value if isinstance(value, dict) else {}
    selected = value.get("items")
    return [identifier for identifier in CAPTAIN_GOODS
            if isinstance(selected, list) and identifier in selected]


def captain_store_access(state):
    if not has_guild(state):
        return {"allowed": False, "authority": None, "reason": "当前尚未加入佣兵团"}
    own = state.get("PlayerGuildInfo") if isinstance(state, dict) else None
    authority = _nonnegative_int((own or {}).get("Authority")) if isinstance(own, dict) else None
    if authority is None:
        return {"allowed": False, "authority": None, "reason": "佣兵团职位信息未确认，请刷新佣兵团信息"}
    if authority not in CAPTAIN_SHOP_AUTHORITIES:
        return {"allowed": False, "authority": authority, "reason": CAPTAIN_SHOP_PERMISSION_MESSAGE}
    return {"allowed": True, "authority": authority, "reason": None}


def _cycle(stamp, reset):
    date = datetime.fromtimestamp(stamp / 1000, UTC)
    if reset == "Day":
        return date.year, date.month, date.day
    year, week, _ = date.isocalendar()
    return year, week


def _reset_at(now_ms, reset):
    date = datetime.fromtimestamp(now_ms / 1000, UTC)
    date = date.replace(hour=0, minute=0, second=0, microsecond=0)
    if reset == "Day":
        result = date + timedelta(days=1)
    else:
        result = date + timedelta(days=7 - date.weekday())
    return int(result.timestamp() * 1000)


class GuildCaptainStoreService:
    def __init__(self, guild):
        self.guild, self.client, self.runtime = guild, guild.client, guild.runtime

    def _records(self):
        state = self.client.account_state or {}
        guild = state.get("GuildData") if isinstance(state, dict) else None
        if guild is not None and not isinstance(guild, dict):
            raise GameError("佣兵团商店记录格式不正确")
        container = (guild or {}).get("GuildStoreRecord") or state.get("GuildStoreRecord") or {}
        if not isinstance(container, dict):
            raise GameError("佣兵团商店记录格式不正确")
        records = container.get("Records")
        if records is None:
            records = []
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise GameError("佣兵团商品列表格式不正确")
        return records

    def _record(self, identifier, records):
        matches = [row for row in records
                   if row.get("StaticID") == identifier and row.get("Store") == "CaptainShop"]
        if len(matches) > 1:
            raise GameError(f"{CAPTAIN_GOODS[identifier]['name']}购买记录重复，请重新登录")
        return matches[0] if matches else None

    def _status_remaining(self, identifier, now_ms):
        state = self.client.account_state or {}
        container = state.get("StatusContainer") if isinstance(state, dict) else None
        if container is None:
            statuses = []
        elif not isinstance(container, dict):
            raise GameError("Buff 状态格式不正确，请刷新佣兵团信息")
        else:
            statuses = container.get("List")
            if statuses is None:
                statuses = []
            if not isinstance(statuses, list):
                raise GameError("Buff 状态格式不正确，请刷新佣兵团信息")
        matches = [row for row in statuses if isinstance(row, dict) and row.get("StaticID") == identifier]
        if not matches:
            return 0
        ends = []
        for row in matches:
            try:
                ends.append(record_stamp(row.get("EndTime")))
            except GameError:
                raise GameError(f"{CAPTAIN_GOODS[identifier]['name']}剩余时间未确认，请刷新佣兵团信息") from None
        return max(0, max(ends) - now_ms)

    def snapshot(self):
        state = self.client.account_state or {}
        now = self.guild.now_ms()
        wall_now = int(time.time() * 1000)
        access = captain_store_access(state)
        guild = state.get("GuildData") if isinstance(state, dict) else None
        container = guild.get("ItemContainer") if isinstance(guild, dict) else None
        inventory = container.get("Items") if isinstance(container, dict) else None
        balances = ({str(row.get("StaticID")): _nonnegative_int(row.get("Count"))
                     for row in inventory if isinstance(row, dict)} if isinstance(inventory, list) else {})
        records = None
        rows = []
        for identifier, meta in _catalog().items():
            error = None
            bought = remaining = remaining_ms = None
            status_pending = identifier in self.runtime.get("captainShopBuffStatusPending", {})
            blocked = identifier in self.runtime.get("captainShopUncertain", {}) or status_pending
            try:
                if not has_guild(state):
                    raise GameError("当前尚未加入佣兵团")
                if records is None:
                    records = self._records()
                record = self._record(identifier, records)
                count = _nonnegative_int(record.get("BuyCount")) if record else 0
                if count is None:
                    raise GameError(f"{meta['name']}购买次数无效，请重新登录")
                bought = count
                limit = meta["limit"]
                if limit is not None and meta["reset"] and count:
                    stamp = record_stamp(record.get("LastResetTime"))
                    if stamp > now:
                        raise GameError(f"{meta['name']}购买记录时间超前，请重新登录")
                    if stamp < 0 or _cycle(stamp, meta["reset"]) != _cycle(now, meta["reset"]):
                        bought = 0
                remaining = None if limit is None else max(0, limit - bought)
                if meta["kind"] == "buff":
                    remaining_ms = self._status_remaining(identifier, now)
            except (GameError, ValueError, TypeError, OverflowError, OSError) as exc:
                error = str(exc)
            retry_at = (self.runtime.get("captainShopRetryAtMs") or {}).get(identifier, 0)
            condition_met = access["allowed"] and error is None and (
                remaining_ms < BUFF_REFRESH_MS if meta["kind"] == "buff"
                else remaining is None or remaining > 0)
            balance = balances.get(meta["currency"])
            affordable = balance is not None and balance >= meta["price"]
            eligible = condition_met and affordable
            rows.append({"id": identifier, **meta, "bought": bought, "remaining": remaining,
                         "remainingMs": remaining_ms, "balance": balance,
                         "conditionMet": condition_met, "affordable": affordable,
                         "eligible": eligible and not blocked,
                         "available": eligible and not blocked and wall_now >= retry_at,
                         "blocked": blocked, "statusPending": status_pending, "error": error})
        return {"access": access,
                "currencies": {key: balances.get(key) for key in ("80", "81")},
                "goods": rows, "running": self.runtime.get("captainShopRunning")}

    def next_delay(self, options):
        # Like the member shop, wait for confirmed balance increases instead
        # of polling. Guild refreshes and settlements publish shared balances.
        selected = set(captain_store_options(options))
        self.runtime["captainShopWatchCurrencies"] = set()
        if not captain_store_access(self.client.account_state)["allowed"] or not selected:
            return None
        now, wall_now, delays = self.guild.now_ms(), int(time.time() * 1000), []
        retry = self.runtime.get("captainShopRetryAtMs") or {}
        status_pending = self.runtime.get("captainShopBuffStatusPending") or {}
        status_retry = self.runtime.get("captainShopStatusSyncRetryAtMs") or {}
        for identifier in selected & set(status_pending):
            delays.append(max(0.0, (status_retry.get(identifier, 0) - wall_now) / 1000))
        for row in self.snapshot()["goods"]:
            if row["id"] not in selected or row["blocked"] or row["error"]:
                continue
            if row["id"] in status_pending:
                continue
            if row["conditionMet"] and not row["affordable"]:
                self.runtime["captainShopWatchCurrencies"].add(row["currency"])
                continue
            if row["eligible"]:
                if wall_now < retry.get(row["id"], 0):
                    delays.append((retry[row["id"]] - wall_now) / 1000)
                else:
                    return 0.0
                continue
            if row["kind"] == "buff" and row["remainingMs"] is not None:
                delays.append(max(0.001, (row["remainingMs"] - BUFF_REFRESH_MS + 1) / 1000))
            elif row["remaining"] == 0 and row["reset"]:
                delays.append(max(0.001, (_reset_at(now, row["reset"]) - now) / 1000))
        return min(delays) if delays else None

    def buy(self, identifier):
        access = captain_store_access(self.client.account_state)
        if not access["allowed"]:
            raise GameError(access["reason"])
        row = next((item for item in self.snapshot()["goods"] if item["id"] == identifier), None)
        if row is None or not row["available"]:
            raise GameError("商品当前不满足自动购买条件")
        return self.guild.buy(identifier, 1, required_currency=row["currency"],
                              required_amount=row["price"])
