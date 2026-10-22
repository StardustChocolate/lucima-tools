"""Game-native guild protocol. Credentials always come from the existing GameClient.

Protocol evidence and units: docs/guild-protocol.md. Mutations are never retried
here: a timeout can mean the server has already committed the operation.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import math
import re
import time
from .game_client import GameClient, GameError, GameMessage
from .game_data import activity_open, public_int

GUILD_ROUTES = (
    ("guild", "GuildHandler.QueryGuildDataForSync"),
    ("full", "GuildHandler.QueryFullGuildData"),
    ("war", "GuildWarHandler.QueryFullGuildWarData"),
    ("status", "ServerStatusHandler.QueryGuildStatus"),
)

# Official client entry order, captured on 2026-10-15. QueryAccountInfo does
# not replace AccountHandler.Login or change the login snapshot's membership.
GUILD_ENTRY_ROUTES = (
    ("account", "AccountHandler.QueryAccountInfo", None),
    ("full", "GuildHandler.QueryFullGuildData", None),
    ("war", "GuildWarHandler.QueryFullGuildWarData", None),
    ("status", "ServerStatusHandler.QueryGuildStatus", None),
    ("invitation", "GuildInvitationHandler.QueryInvitation", {"IsQueryByGuild": 1}),
)

# PlayerGuildInfo static limits: .cctor RVA 0x2193dcc; costs and request
# parameters confirmed by the 2026-10-15 DonateGold / DonateCourage captures.
GUILD_DONATIONS = {
    "gold": {"name": "金币", "route": "GuildHandler.DonateGold", "item": "1",
             "cost": 5000, "limit": 10, "field": "DayDonateGoldCount", "reserve": 200000},
    "courage": {"name": "英勇证明", "route": "GuildHandler.DonateCourage", "item": "28",
                "cost": 1, "limit": 3, "field": "DayDonateCourageCount", "reserve": 10},
}
# PlayerGuildInfo.Authority: 1 leader, 2 deputy, 3 regular member.
CAPTAIN_SHOP_AUTHORITIES = frozenset({1, 2})
CAPTAIN_SHOP_PERMISSION_MESSAGE = "团长商店仅限团长和副团长购买"
CAPTAIN_BUFF_STATUS_SYNC_MS = 60 * 1000
# Default reserves match sd_Quest GuildDonatedGold / GuildDonatedCourage,
# ConditionCount 200000 / 10 (item IDs 1 / 28), catalog 115657.


def donation_options(value=None):
    value = value if isinstance(value, dict) else {}
    return {key: bool(value.get(key, True)) for key in GUILD_DONATIONS}


def donation_reserves(value=None):
    """Return per-resource balances that automatic donation must keep.

    Missing or invalid values use the weekly quest requirements. Explicit zero
    remains valid. Numeric strings are also accepted from saved form settings.
    """
    value = value if isinstance(value, dict) else {}
    reserve = value.get("reserve")
    reserve = reserve if isinstance(reserve, dict) else {}
    result = {}
    for key, rule in GUILD_DONATIONS.items():
        item = reserve.get(key, rule["reserve"])
        if isinstance(item, bool):
            result[key] = rule["reserve"]
        elif isinstance(item, int) and item >= 0:
            result[key] = item
        elif isinstance(item, str) and item.strip().isdigit():
            result[key] = int(item.strip())
        else:
            result[key] = rule["reserve"]
    return result


def _nonnegative_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class CaptainBuffStatusPending(GameError):
    """The purchase committed, but its resulting timed status is not confirmed yet."""


class CaptainShopInsufficientCurrency(GameError):
    """A fresh guild snapshot showed the selected item is still unaffordable."""

    def __init__(self, currency, balance, required):
        self.currency = currency
        self.balance = balance
        self.required = required
        super().__init__(f"佣兵团公共货币 {currency} 不足：当前 {balance if balance is not None else '未确认'}，需要 {required}")


def has_guild(state):
    info = (state or {}).get("PlayerGuildInfo") or {}
    return isinstance(info, dict) and bool(object_id(info.get("GID")))


def _timestamp(value):
    if isinstance(value, dict):
        value = value.get("$date")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def checked_in_today(info, now_ms):
    stamp = _timestamp((info or {}).get("LastCheckInTime"))
    # UTC midnight is 08:00 in the game's +08 timezone, as for calendar sign-in.
    return stamp is not None and stamp // 86_400_000 == now_ms // 86_400_000


def positive_int(value, label, maximum):
    if isinstance(value, bool):
        raise GameError(f"{label}必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise GameError(f"{label}必须是整数") from None
    if str(number) != str(value).strip() or not 1 <= number <= maximum:
        raise GameError(f"{label}必须为 1～{maximum} 的整数")
    return number


def describe_error(error):
    message = str(error)
    if isinstance(error, GameMessage):
        if message == "OverCount":
            return "已达到游戏服务器允许的次数或数量上限（OverCount）。"
        if message == "CantCheckIn":
            return "游戏服务器未允许本次签到（CantCheckIn），请查询佣兵团状态。"
        if message.startswith("E0001"):
            return "游戏服务器处理请求失败（" + message + "）"
    return message


def is_auth_error(error):
    if isinstance(error, GameMessage) and GameClient._looks_like_auth_failure(error):
        return True
    return any(marker in str(error).lower() for marker in (
        "expired", "session", "not login", "not logged", "login required",
        "duplicate login", "duplicatelogin", "重复登录",
        "未登录", "登录失效", "登录过期", "会话失效", "凭证失效",
    ))


def _response_error(response):
    rejected = (response.get("_isErrorResponse") is True
                or response.get("Success") is False or response.get("success") is False
                or bool(response.get("Error")) or bool(response.get("error")))
    if not rejected:
        return None
    # These are message sources, not independent failure flags. In particular,
    # the presence of ErrorCode or Reason alone does not imply failure.
    for key in ("_formattedMessage", "message", "originalError", "Error", "error",
                "ErrorCode", "errorCode", "Reason", "Message"):
        value = response.get(key)
        if value is not None and value != "" and value is not False:
            return str(value)
    return "游戏拒绝了请求"


def quest_response_summary(response):
    """Describe a parsed reply without equating step completion with a reward."""
    results = response.get("Results") or []
    has_data = any(
        any(bool(value) for value in (item.get("Drop") or {}).values())
        or bool(item.get("Quests")) or bool(item.get("GuildQuestDataList"))
        or item.get("HasMail") is True
        for item in results
    )

    def shape(value):
        if value is None:
            return "null"
        if isinstance(value, list):
            return f"数组（{len(value)} 项）"
        if isinstance(value, dict):
            return f"对象（{len(value)} 个字段）"
        if isinstance(value, bool):
            return "布尔值"
        if isinstance(value, (int, float)):
            return "数值"
        return "文本"

    def fields(value):
        # Only field names/types/counts leave the protocol layer, never AID,
        # SessionID, token values or complete account data.
        pairs = list(value.items())[:24]
        return "；".join(f"{str(key)[:64]}：{shape(item)}" for key, item in pairs) or "空对象"

    lines = ["顶层：" + fields(response)]
    if response.get("Results") is None:
        lines.append("Results 缺失或为 null，按原包规则作为空列表处理。")
    for index, item in enumerate(results[:3], 1):
        lines.append(f"Results 第 {index} 项：" + fields(item))
    return {
        "state": "success" if has_data else "received",
        "message": "已返回奖励或任务数据，实际到账以游戏记录为准。" if has_data else "",
        "responseFields": "\n".join(lines)[:2400],
    }


def object_id(value):
    if isinstance(value, dict):
        value = value.get("$oid")
    return str(value) if isinstance(value, (str, int)) and value else ""


def guild_identity(response):
    """Only accept explicit live guild data, never infer membership from cached login."""
    if "GuildData" not in response:
        raise GameError("佣兵团查询缺少 GuildData，无法确认当前佣兵团，已停止")
    guild = response["GuildData"]
    if guild is None:
        return None
    if not isinstance(guild, dict):
        raise GameError("佣兵团数据格式不正确，已停止")
    info = guild.get("Info")
    if not isinstance(info, dict) or not info.get("Name"):
        raise GameError("佣兵团查询缺少名称，无法确认目标，已停止")
    return {"name": str(info["Name"]), "id": object_id(info.get("_id")), "data": guild}


def guild_date(value):
    default = "0001/1/1 0:0:0"
    if isinstance(value, str):
        return value if value and not value.startswith("0001/") else default
    if isinstance(value, dict):
        value = value.get("$date")
    if not isinstance(value, (int, float)) or value < 0:
        return default
    try:
        date = datetime.fromtimestamp(value / 1000, timezone.utc)
        return f"{date.year}/{date.month}/{date.day} {date.hour}:{date.minute}:{date.second}"
    except (OverflowError, ValueError, OSError):
        raise GameError("佣兵团商店时间格式无效，请重新查询") from None


def commodity_record(item_id, record=None):
    record = record or {}
    return {
        "_id": object_id(record.get("_id")), "StaticID": item_id,
        "Store": record.get("Store") or "CaptainShop",
        "BuyCount": record.get("BuyCount") or 0,
        "FreeBuyCount": record.get("FreeBuyCount") or 0,
        "GuaranteedCount": record.get("GuaranteedCount") or 0,
        "SummonRoleID": record.get("SummonRoleID") or "",
        "SummonRoleIDs": record.get("SummonRoleIDs") or [],
        "LastBuyTime": guild_date(record.get("LastBuyTime")),
        "LastResetTime": guild_date(record.get("LastResetTime")),
    }


class GuildService:
    def __init__(self, client: GameClient, runtime=None, reserves=None):
        self.client = client
        self.runtime = runtime if runtime is not None else {}
        self.donation_reserves = donation_reserves({"reserve": reserves})

    def now_ms(self):
        now = int(time.time() * 1000)
        # Status polling does not take the game-request lock; copy the clock
        # before a concurrent successful login clears the old runtime.
        runtime = dict(self.runtime)
        if "serverTimeMs" in runtime:
            return runtime["serverTimeMs"] + now - runtime["serverTimeSyncedAtMs"]
        try:
            return int(float(self.client.account_state["Utc"]) * 1000) + now - runtime["stateSyncedAtMs"]
        except (KeyError, TypeError, ValueError, OverflowError):
            return now

    def snapshot(self):
        state = self.client.account_state or {}
        joined = has_guild(state)
        guild = state.get("GuildData") or {}
        info = (guild.get("Info") or {}) if isinstance(guild, dict) else {}
        if not isinstance(info, dict):
            info = {}
        own = state.get("PlayerGuildInfo") or {}
        record = (guild.get("Record") or {}) if isinstance(guild, dict) else {}
        last_count = record.get("LastCheckInCount") if isinstance(record, dict) else None
        # GuildHallPanel.OnDayCheckReward also requires yesterday's attendance.
        can_claim = (isinstance(last_count, (int, float)) and not isinstance(last_count, bool)
                     and last_count > 0 and own.get("CanLastDayCheckReward") is True)
        icon = re.fullmatch(r"UI/Image/UI_iconz\[(UI80_Guild_Sign_\d+)\]", str(info.get("Icon") or ""))
        frame = re.fullmatch(r"GuildFrame\d+", str(info.get("FrameIcon") or ""))
        return {
            "joined": joined,
            "name": info.get("Name") if joined else None,
            "level": info.get("GuildLV") if joined else None,
            "memberCount": info.get("MemberCount") if joined else None,
            "icon": f"/assets/guild/{icon[1]}.png" if joined and icon else None,
            "frame": f"/assets/guild/{frame[0]}.png" if joined and frame else None,
            "checkedIn": joined and checked_in_today(own, self.now_ms()),
            "canClaim": joined and can_claim,
            "updatedAt": self.runtime.get("updatedAtMs"),
            "loading": bool(self.runtime.get("loading")),
            "claiming": bool(self.runtime.get("claiming")),
            "claimError": self.runtime.get("claimError"),
            "donations": self.donation_status(),
            "donating": self.runtime.get("donating"),
            "donationErrors": dict(self.runtime.get("donationErrors") or {}),
            "error": self.runtime.get("error"),
        }

    def donation_status(self):
        """Read today's confirmed counters and affordable quantities, without queries."""
        state = self.client.account_state or {}
        own = state.get("PlayerGuildInfo") or {}
        ready = has_guild(state) and checked_in_today(own, self.now_ms())
        items = (state.get("ItemContainer") or {}).get("Items") or []
        balances = {str(item.get("StaticID")): _nonnegative_int(item.get("Count")) or 0
                    for item in items if isinstance(item, dict)}
        result = {}
        for key, rule in GUILD_DONATIONS.items():
            donated = _nonnegative_int(own.get(rule["field"])) if ready else None
            remaining = max(0, rule["limit"] - donated) if donated is not None else 0
            available = max(0, balances.get(rule["item"], 0) - self.donation_reserves.get(key, 0))
            result[key] = {"donated": donated, "limit": rule["limit"],
                           "count": min(remaining, available // rule["cost"])}
        return result

    def donate_if_due(self, kind):
        rule = GUILD_DONATIONS[kind]
        before = self.donation_status()[kind]
        count = before["count"]
        if not count:
            return 0
        self._call(rule["route"], {"ItemID": rule["item"], "Count": count})
        after = self.donation_status()[kind]
        if after["donated"] != before["donated"] + count:
            raise GameError("佣兵团捐赠响应未确认次数，等待下次同步后确认")
        return count

    def _cache(self, result, route=None):
        state = self.client.account_state
        if not isinstance(state, dict):
            return
        currency_increases = set()
        apply_statuses = getattr(self.client, "_apply_account_status_updates", None)
        if callable(apply_statuses):
            apply_statuses(result)
        if route == "ServerStatusHandler.QueryGuildStatus":
            pending = self.runtime.get("captainShopBuffStatusPending") or {}
            updates = result.get("AccountStatusList") if isinstance(result, dict) else None
            for status in updates if isinstance(updates, list) else []:
                if not isinstance(status, dict) or not object_id(status.get("_id")):
                    continue
                identifier = status.get("StaticID")
                expected = pending.get(identifier)
                end = _timestamp(status.get("EndTime"))
                after = expected.get("afterMs", 0) if isinstance(expected, dict) else None
                if after is not None and end is not None and end > after:
                    pending.pop(identifier, None)
                    (self.runtime.get("captainShopStatusSyncRetryAtMs") or {}).pop(identifier, None)
        try:
            stamp = int(float(result["Utc"]) * 1000)
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        else:
            self.runtime.update(serverTimeMs=stamp, serverTimeSyncedAtMs=int(time.time() * 1000))
        if isinstance(result.get("Info"), dict):
            state["Info"] = {**(state.get("Info") or {}), **deepcopy(result["Info"])}
        guild = result.get("GuildData")
        if isinstance(guild, dict):
            old_guild = state.get("GuildData")
            old_items = ((old_guild if isinstance(old_guild, dict) else {}).get("ItemContainer") or {}).get("Items")
            old_balances = {str(item.get("StaticID")): _nonnegative_int(item.get("Count"))
                            for item in old_items or [] if isinstance(item, dict)}
            gid = object_id((guild.get("Info") or {}).get("_id"))
            if (has_guild(state) and gid
                    and gid != object_id(state["PlayerGuildInfo"].get("GID"))):
                raise GameError("佣兵团信息与登录快照不一致，请重新登录")
            state["GuildData"] = deepcopy(guild)
            self.runtime["updatedAtMs"] = int(time.time() * 1000)
            # SelfGuildInfo may be a default object. Find our own member by
            # CUID / player object ID; never replace state with that default.
            own = state.get("Info") or {}
            for member in guild.get("MemberList") or []:
                if not isinstance(member, dict):
                    continue
                player = member.get("PlayerInfo") or {}
                match = ((own.get("CUID") and player.get("CUID") == own["CUID"])
                         or (object_id(own.get("_id")) and object_id(player.get("_id")) == object_id(own["_id"])))
                info = member.get("PlayerGuildInfo")
                if match and isinstance(info, dict):
                    state["PlayerGuildInfo"] = deepcopy(info)
                    break
            new_items = ((guild.get("ItemContainer") or {}).get("Items") or [])
            currency_increases.update(
                str(item.get("StaticID")) for item in new_items if isinstance(item, dict)
                and str(item.get("StaticID")) in {"80", "81"}
                and _nonnegative_int(item.get("Count")) is not None
                and _nonnegative_int(item.get("Count")) > (old_balances.get(str(item.get("StaticID"))) or 0))
        nested = result.get("PlayerGuildData") or {}
        info = nested.get("PlayerGuildInfo") if isinstance(nested, dict) else None
        if not isinstance(info, dict):
            info = result.get("PlayerGuildInfo")
        # RewardQuest can return a default PlayerGuildInfo without GID (cdp-123,
        # 125, 126). It is not this player's authoritative member record.
        if (isinstance(info, dict) and object_id(info.get("GID"))
                == object_id((state.get("PlayerGuildInfo") or {}).get("GID"))
                and object_id(info.get("GID"))):
            state["PlayerGuildInfo"] = deepcopy(info)
        guild = state.get("GuildData")
        if not isinstance(guild, dict):
            return
        gid = object_id((state.get("PlayerGuildInfo") or {}).get("GID"))
        record = guild.get("Record") or {}
        week = _nonnegative_int(record.get("LastResetQuestWeek"))
        # Public tier progress appears both at the top level and inside Results.
        # Replace by StaticID only within the active guild and server week.
        parts = [result] + [part for part in result.get("Results") or [] if isinstance(part, dict)]
        for part in parts:
            # Captain-shop costs and GuildDropResult settle the guild's shared
            # inventory. Only NowItem is an authoritative balance, never Item.
            public_drop = part.get("GuildDropResult")
            entries = list(public_drop.get("Items") or []) if isinstance(public_drop, dict) else []
            if route == "GuildItemHandler.BuyGuildCommodity" and isinstance(part.get("CostItems"), list):
                entries = part["CostItems"] + entries
            for entry in entries:
                item = entry.get("NowItem") if isinstance(entry, dict) else None
                if (not isinstance(item, dict) or not item.get("StaticID")
                        or _nonnegative_int(item.get("Count")) is None):
                    continue
                container = guild.setdefault("ItemContainer", {})
                if not isinstance(container, dict):
                    continue
                inventory = container.setdefault("Items", [])
                if not isinstance(inventory, list):
                    continue
                current = next((row for row in inventory if isinstance(row, dict)
                                and str(row.get("StaticID")) == str(item["StaticID"])), None)
                previous_count = _nonnegative_int(current.get("Count")) if current else 0
                new_count = _nonnegative_int(item.get("Count"))
                if str(item["StaticID"]) in {"80", "81"} and new_count is not None and new_count > (previous_count or 0):
                    currency_increases.add(str(item["StaticID"]))
                if current is None:
                    inventory.append(deepcopy(item))
                else:
                    current.update(deepcopy(item))
            for quest in part.get("GuildQuestDataList") or []:
                if (not isinstance(quest, dict) or not quest.get("StaticID") or week is None
                        or quest.get("Week") != week or object_id(quest.get("GID")) != gid):
                    continue
                rows = guild.setdefault("GuildQuestList", [])
                rows[:] = [row for row in rows if row.get("StaticID") != quest["StaticID"]]
                rows.append(deepcopy(quest))
        callback = getattr(self.client, "guild_currency_increase_callback", None)
        if currency_increases and callable(callback):
            callback(currency_increases)

    def _call(self, route, extra=None):
        result = self.client.call(route, self.client._auth_data(extra))
        if isinstance(result, str) and result.strip():
            raise GameMessage(result.strip())
        if not isinstance(result, dict):
            raise GameError("游戏返回了无法识别的响应，结果未确认，请查询后再操作")
        error = _response_error(result)
        if error is not None:
            raise GameMessage(error)
        self._cache(result, route)
        return result

    def enter(self):
        """Refresh the entire joined-guild entry flow, then check in if needed."""
        if not has_guild(self.client.account_state):
            return {"skipped": True, "checkedIn": False}
        for key, route, extra in GUILD_ENTRY_ROUTES:
            if key == "war":
                # QueryFullGuildDataWithGuildWar: Version_GVG must be open and
                # the freshly queried GuildLV must reach GuildWarLessLV.
                state = self.client.account_state
                level = _nonnegative_int((state["GuildData"].get("Info") or {}).get("GuildLV"))
                if (level is None or level < public_int("GuildWarLessLV")
                        or not activity_open("Version_GVG", state, now=self.now_ms() / 1000)):
                    continue
            try:
                response = self._call(route, extra)
            except GameMessage as error:
                # A stale unlock condition must not block ordinary guild entry.
                # Authentication and transport failures still propagate.
                if key == "war" and error.msg == "Low GuildLV":
                    continue
                raise
            if key == "full":
                guild = response.get("GuildData")
                if not isinstance(guild, dict) or not (guild.get("Info") or {}).get("Name"):
                    raise GameError("佣兵团查询缺少完整信息，请刷新登录快照后重试")
                gid = object_id((guild.get("Info") or {}).get("_id"))
                if gid and gid != object_id(self.client.account_state["PlayerGuildInfo"].get("GID")):
                    raise GameError("佣兵团信息与登录快照不一致，请重新登录")
        return {"checkedIn": self.checkin_if_due()}

    def checkin_if_due(self):
        own = self.client.account_state.get("PlayerGuildInfo") or {}
        if not has_guild(self.client.account_state):
            return False
        if _timestamp(own.get("LastCheckInTime")) is None:
            raise GameError("佣兵团签到时间缺失，无法确认今日状态，请刷新后重试")
        if checked_in_today(own, self.now_ms()):
            return False
        self._call("GuildHandler.GuildMemberCheckIn")
        if not checked_in_today(self.client.account_state.get("PlayerGuildInfo"), self.now_ms()):
            raise GameError("佣兵团报到响应未确认今日状态，将查询后再检查")
        return True

    def claim_daily_if_due(self):
        state = self.snapshot()
        if not state["joined"] or not state["checkedIn"] or not state["canClaim"]:
            return False
        self.attendance()
        if (self.client.account_state.get("PlayerGuildInfo") or {}).get("CanLastDayCheckReward") is not False:
            raise GameError("佣兵团日奖励响应未确认领取状态，将查询后再检查")
        return True

    def query(self):
        return self._call("GuildHandler.QueryGuildDataForSync")

    def overview(self):
        out = {}
        for key, route in GUILD_ROUTES:
            try:
                out[key] = self._call(route)
            except (GameError, GameMessage) as error:
                if is_auth_error(error) or not self.client.is_connected():
                    raise
                out[key] = {"error": describe_error(error)}
        return out

    def checkin(self):
        return self.enter()

    def attendance(self):
        return self._call("GuildHandler.GuildMemberDayCheckReward")

    def buy(self, item_id, count=1, *, required_currency=None, required_amount=None):
        # BuyGuildCommodity consumes a complete commodity Record, not ItemID.
        allowed = {"GuildBox", "GuildHeroExp", "GuildMoneyBonus", "GuildEnergy30", "GuildEnergy50"}
        if item_id not in allowed or positive_int(count, "购买次数", 1) != 1:
            raise GameError("无效的团长商店商品")
        full = self._call("GuildHandler.QueryFullGuildData")
        guild = full.get("GuildData") or {}
        if not isinstance(guild, dict):
            raise GameError("佣兵团数据格式不正确，未发送购买请求")
        if required_currency is not None or required_amount is not None:
            if (required_currency not in {"80", "81"}
                    or _nonnegative_int(required_amount) is None or required_amount <= 0):
                raise GameError("团长商店价格校验参数无效，未发送购买请求")
            items = (guild.get("ItemContainer") or {}).get("Items")
            balance = next((_nonnegative_int(row.get("Count")) for row in items or []
                            if isinstance(row, dict) and str(row.get("StaticID")) == required_currency), None)
            if balance is None or balance < required_amount:
                raise CaptainShopInsufficientCurrency(required_currency, balance, required_amount)
        state = self.client.account_state or {}
        profile = state.get("Info") if isinstance(state, dict) else None
        if not isinstance(profile, dict):
            raise GameError("当前账号身份未确认，未发送购买请求")
        membership = state.get("PlayerGuildInfo")
        guild_info = guild.get("Info")
        if not isinstance(membership, dict) or not isinstance(guild_info, dict):
            raise GameError("佣兵团成员职位未确认，未发送购买请求")
        current_gid = object_id(membership.get("GID"))
        full_gid = object_id(guild_info.get("_id"))
        if not current_gid or full_gid != current_gid:
            raise GameError("佣兵团信息与登录快照不一致，未发送购买请求")
        cuid, player_id = profile.get("CUID"), object_id(profile.get("_id"))
        if not cuid and not player_id:
            raise GameError("当前账号身份未确认，未发送购买请求")
        members = guild.get("MemberList")
        if not isinstance(members, list):
            raise GameError("佣兵团成员职位未确认，未发送购买请求")
        own_members = []
        for member in members:
            if not isinstance(member, dict):
                continue
            player = member.get("PlayerInfo")
            if not isinstance(player, dict):
                continue
            if ((cuid and player.get("CUID") == cuid)
                    or (player_id and object_id(player.get("_id")) == player_id)):
                own_members.append(member)
        if len(own_members) != 1:
            raise GameError("当前账号的佣兵团职位未确认，未发送购买请求")
        own_guild_info = own_members[0].get("PlayerGuildInfo")
        authority = _nonnegative_int(own_guild_info.get("Authority")) if isinstance(own_guild_info, dict) else None
        if authority is None:
            raise GameError("当前账号的佣兵团职位未确认，未发送购买请求")
        if authority not in CAPTAIN_SHOP_AUTHORITIES:
            raise GameError(CAPTAIN_SHOP_PERMISSION_MESSAGE)
        store = guild.get("GuildStoreRecord") or full.get("GuildStoreRecord") or {}
        if not isinstance(store, dict):
            raise GameError("佣兵团商店记录格式不正确，未发送购买请求")
        records = store.get("Records")
        if records is None:
            records = []
        if not isinstance(records, list):
            raise GameError("佣兵团商品列表格式不正确，未发送购买请求")
        record = next((r for r in records if isinstance(r, dict)
                       and r.get("StaticID") == item_id and r.get("Store") == "CaptainShop"), None)
        request_record = commodity_record(item_id, record)
        before_count = _nonnegative_int(request_record.get("BuyCount"))
        if before_count is None:
            raise GameError("佣兵团商品购买记录次数无效，未发送购买请求")
        buff_after = None
        if item_id in {"GuildMoneyBonus", "GuildHeroExp"}:
            statuses = ((state.get("StatusContainer") or {}).get("List") or [])
            previous_end = max((_timestamp(status.get("EndTime")) or 0 for status in statuses
                                if isinstance(status, dict) and status.get("StaticID") == item_id), default=0)
            # Capture the old expiry before the purchase response can update it.
            buff_after = max(previous_end, self.now_ms())
        result = self._call("GuildItemHandler.BuyGuildCommodity", {
            "Record": request_record, "Count": 0, "ItemIndex": -1, "LoginType": "ID_Password",
            "NewErolabs": 0, "Platform": "Android", "SelectRoleID": ""})
        returned = result.get("CommodityRecord")
        if (not isinstance(returned, dict) or returned.get("StaticID") != item_id
                or returned.get("Store") != "CaptainShop"
                or _nonnegative_int(returned.get("BuyCount")) != before_count + 1
                or (request_record["_id"] and object_id(returned.get("_id")) != request_record["_id"])):
            raise GameError("购买结果未确认，请重新登录后确认；为避免重复扣费，未自动重试")

        state = self.client.account_state
        guild_state = state.get("GuildData") if isinstance(state, dict) else None
        if isinstance(guild_state, dict):
            cached_store = guild_state.get("GuildStoreRecord")
            if not isinstance(cached_store, dict):
                cached_store = {"Records": []}
                guild_state["GuildStoreRecord"] = cached_store
            cached_records = cached_store.get("Records")
            if not isinstance(cached_records, list):
                cached_records = []
                cached_store["Records"] = cached_records
            returned_id = object_id(returned.get("_id"))
            index = next((i for i, row in enumerate(cached_records)
                          if isinstance(row, dict) and (
                              (returned_id and object_id(row.get("_id")) == returned_id)
                              or (row.get("StaticID") == item_id and row.get("Store") == "CaptainShop"))), None)
            if index is None:
                cached_records.append(deepcopy(returned))
            else:
                cached_records[index] = deepcopy(returned)
        if buff_after is not None:
            statuses = ((state.get("StatusContainer") or {}).get("List") or [])
            if any(isinstance(status, dict) and status.get("StaticID") == item_id
                   and (_timestamp(status.get("EndTime")) or 0) > buff_after for status in statuses):
                return result
            pending = self.runtime.setdefault("captainShopBuffStatusPending", {})
            pending[item_id] = {"afterMs": buff_after}
            try:
                self._call("ServerStatusHandler.QueryGuildStatus")
            except Exception as error:
                self.runtime.setdefault("captainShopStatusSyncRetryAtMs", {})[item_id] = (
                    int(time.time() * 1000) + CAPTAIN_BUFF_STATUS_SYNC_MS)
                raise CaptainBuffStatusPending("购买已成功，Buff 状态待同步；只会重试状态查询") from error
            if item_id in pending:
                self.runtime.setdefault("captainShopStatusSyncRetryAtMs", {})[item_id] = (
                    int(time.time() * 1000) + CAPTAIN_BUFF_STATUS_SYNC_MS)
                raise CaptainBuffStatusPending("购买已成功，但 Buff 状态未返回；只会重试状态查询")
        return result

    def quest_ids(self):
        snapshot = self.query()
        current = guild_identity(snapshot)
        if current is None:
            raise GameError("当前账号尚未加入佣兵团")
        record = current["data"].get("Record")
        if record is None:
            record = {}
        if not isinstance(record, dict):
            raise GameError("佣兵团任务记录格式不正确")
        ids = record.get("GuildQuestIDList")
        if ids is None:
            ids = []
        if not isinstance(ids, list) or len(ids) > 512:
            raise GameError("佣兵团任务列表格式或数量异常，请重新查询")
        result = []
        for quest_id in ids:
            if isinstance(quest_id, bool) or not isinstance(quest_id, (str, int)):
                raise GameError("佣兵团任务 ID 格式不正确")
            quest_id = str(quest_id).strip()
            if not quest_id or len(quest_id) > 256:
                raise GameError("佣兵团任务 ID 为空或过长")
            if quest_id not in result:
                result.append(quest_id)
        return {"guildName": current["name"], "questIds": result}

    def claim_quest(self, quest_id):
        if not isinstance(quest_id, str) or not quest_id.strip() or len(quest_id) > 256:
            raise GameError("任务 ID 无效")
        response = self._call("QuestHandler.RewardQuest", {
            "RewardQuestInfos": [{"ID": quest_id, "Index": 0}]})
        results = response.get("Results")
        # Reference parser 0x564c84 defaults missing/null Results to an empty list.
        # A completed request is not evidence that rewards were granted.
        if results is None:
            results = []
        if not isinstance(results, list):
            raise GameError("领奖响应的 Results 不是数组，结果未确认；请查询游戏后再操作")
        for result in results:
            if not isinstance(result, dict):
                raise GameError("领奖响应格式异常，结果未确认；已停止后续请求")
            error = _response_error(result)
            if error is not None:
                raise GameMessage(error)
            # Reference parser 0x564ed4 accepts null/empty optional fields as well.
            for key, kind in (("Drop", dict), ("Quests", list),
                              ("GuildQuestDataList", list), ("HasMail", bool)):
                value = result.get(key)
                if value is not None and not isinstance(value, kind):
                    raise GameError(f"领奖响应的 {key} 类型异常，结果未确认；已停止后续请求")
                if kind is list and value is not None and any(not isinstance(item, dict) for item in value):
                    raise GameError(f"领奖响应的 {key} 包含无效记录，结果未确认；已停止后续请求")
        # GameClient already merges nested Results balances and quest updates.
        return response
