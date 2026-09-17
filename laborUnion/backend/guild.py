"""Game-native guild protocol. Credentials always come from the existing GameClient.

Protocol evidence and units: docs/guild-protocol.md. Mutations are never retried
here: a timeout can mean the server has already committed the operation.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
from .game_client import GameClient, GameError, GameMessage

GUILD_ROUTES = (
    ("guild", "GuildHandler.QueryGuildDataForSync"),
    ("full", "GuildHandler.QueryFullGuildData"),
    ("war", "GuildWarHandler.QueryFullGuildWarData"),
    ("status", "ServerStatusHandler.QueryGuildStatus"),
)


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
            return "已达到游戏服务器允许的捐献次数或数量上限（OverCount），已停止。"
        if message == "CantCheckIn":
            return "游戏服务器未允许本次签到（CantCheckIn），请查询工会状态。"
        if message.startswith("E0001"):
            return "游戏服务器处理请求失败（" + message + "）"
    return message


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
        raise GameError("工会查询缺少 GuildData，无法确认当前工会，已停止")
    guild = response["GuildData"]
    if guild is None:
        return None
    if not isinstance(guild, dict):
        raise GameError("工会数据格式不正确，已停止")
    info = guild.get("Info")
    if not isinstance(info, dict) or not info.get("Name"):
        raise GameError("工会查询缺少名称，无法确认目标，已停止")
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
        raise GameError("工会商店时间格式无效，请重新查询") from None


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
    def __init__(self, client: GameClient):
        self.client = client

    def _call(self, route, extra=None):
        result = self.client.call(route, self.client._auth_data(extra))
        if isinstance(result, str) and result.strip():
            raise GameMessage(result.strip())
        if not isinstance(result, dict):
            raise GameError("游戏返回了无法识别的响应，结果未确认，请查询后再操作")
        error = _response_error(result)
        if error is not None:
            raise GameMessage(error)
        return result

    def query(self):
        return self._call("GuildHandler.QueryGuildDataForSync")

    def overview(self):
        out = {}
        for key, route in GUILD_ROUTES:
            try:
                out[key] = self._call(route)
            except (GameError, GameMessage) as error:
                out[key] = {"error": describe_error(error)}
        return out

    def checkin(self):
        # The original attendance flow synchronizes the three guild views first.
        for _, route in GUILD_ROUTES[1:]:
            self._call(route)
        return self._call("GuildHandler.GuildMemberCheckIn")

    def attendance(self):
        return self._call("GuildHandler.GuildMemberDayCheckReward")

    def donate(self, amount=50000, courage=False):
        if courage:
            payload = {"ItemID": "28", "Count": 1}
        else:
            amount = positive_int(amount, "金币数量", 50000)
            if amount % 5000:
                raise GameError("金币数量必须为 5,000 的倍数")
            # Count is donation units (5,000 gold each), not the gold amount.
            payload = {"ItemID": "1", "Count": amount // 5000}
        result = self._call("GuildHandler.DonateCourage" if courage else "GuildHandler.DonateGold", payload)
        if not (isinstance(result.get("PlayerGuildInfo"), dict) and result["PlayerGuildInfo"]
                or isinstance(result.get("CostItems"), list) and result["CostItems"]
                or isinstance(result.get("Drop"), dict) and result["Drop"]):
            raise GameError("捐献响应未确认结果，已停止；请查询工会和金币余额，勿直接重复提交")
        if isinstance(result.get("PlayerGuildInfo"), dict) and self.client.account_state is not None:
            self.client.account_state["PlayerGuildInfo"] = deepcopy(result["PlayerGuildInfo"])
        return result

    def aid_request(self, item_id):
        return self._call("GuildHandler.RequestGuildAid", {"ItemID": str(item_id)})

    def aid_support(self, item_id):
        return self._call("GuildHandler.SupportGuildAid", {"GuildAidItemInfoID": str(item_id)})

    def buy(self, item_id, count=1):
        # BuyGuildCommodity consumes a complete commodity Record, not ItemID.
        allowed = {"GuildBox", "GuildHeroExp", "GuildMoneyBonus", "GuildEnergy30", "GuildEnergy50"}
        if item_id not in allowed or positive_int(count, "购买次数", 1) != 1:
            raise GameError("无效的会长商店商品")
        full = self._call("GuildHandler.QueryFullGuildData")
        guild = full.get("GuildData") or {}
        store = guild.get("GuildStoreRecord") or full.get("GuildStoreRecord") or {}
        records = store.get("Records", []) if isinstance(store, dict) else []
        record = next((r for r in records if isinstance(r, dict)
                       and r.get("StaticID") == item_id and r.get("Store") == "CaptainShop"), None)
        record = commodity_record(item_id, record)
        return self._call("GuildItemHandler.BuyGuildCommodity", {
            "Record": record, "Count": 0, "ItemIndex": -1, "LoginType": "ID_Password",
            "NewErolabs": 0, "Platform": "Android", "SelectRoleID": ""})

    def quest_ids(self):
        snapshot = self.query()
        current = guild_identity(snapshot)
        if current is None:
            raise GameError("当前账号尚未加入工会")
        record = current["data"].get("Record") or {}
        if not isinstance(record, dict):
            raise GameError("工会任务记录格式不正确")
        ids = record.get("GuildQuestIDList") or []
        if not isinstance(ids, list) or len(ids) > 512:
            raise GameError("工会任务列表格式或数量异常，请重新查询")
        result = []
        for quest_id in ids:
            if isinstance(quest_id, bool) or not isinstance(quest_id, (str, int)):
                raise GameError("工会任务 ID 格式不正确")
            quest_id = str(quest_id).strip()
            if not quest_id or len(quest_id) > 256:
                raise GameError("工会任务 ID 为空或过长")
            if quest_id not in result:
                result.append(quest_id)
        return {"guildName": current["name"], "questIds": result}

    def claim_quest(self, quest_id):
        if not isinstance(quest_id, str) or not quest_id.strip() or len(quest_id) > 256:
            raise GameError("任务 ID 无效")
        response = self._call("QuestHandler.RewardQuest", {
            "RewardQuestInfos": [{"ID": quest_id, "Index": 0}]})
        results = response.get("Results")
        # Original 0x564c84 defaults missing/null Results to an empty list.
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
            # Original 0x564ed4 accepts null/empty optional fields as well.
            for key, kind in (("Drop", dict), ("Quests", list),
                              ("GuildQuestDataList", list), ("HasMail", bool)):
                value = result.get(key)
                if value is not None and not isinstance(value, kind):
                    raise GameError(f"领奖响应的 {key} 类型异常，结果未确认；已停止后续请求")
                if kind is list and value is not None and any(not isinstance(item, dict) for item in value):
                    raise GameError(f"领奖响应的 {key} 包含无效记录，结果未确认；已停止后续请求")
        # RewardQuest wraps each drop in Results; GameClient updates only the
        # top-level Drop. Reuse its absolute NowItem updater for every result.
        for result in results:
            self.client._apply_now_items(result)
        if isinstance(response.get("PlayerGuildInfo"), dict) and self.client.account_state is not None:
            self.client.account_state["PlayerGuildInfo"] = deepcopy(response["PlayerGuildInfo"])
        return response

    def quest_reward(self, quest_id=None):
        return self._call("QuestHandler.RewardGuildQuest")

    def search(self, name):
        name = str(name or "").strip()
        if not name:
            raise GameError("请填写目标工会名称")
        return self._call("GuildHandler.SearchGuild", {
            "Name": name, "AdmitType": "OpenAutomatic", "SearchWithType": 0})

    def join(self, guild_id):
        guild_id = object_id(guild_id)
        if not guild_id:
            raise GameError("工会 ID 不能为空")
        return self._call("GuildInvitationHandler.JoinGuild", {"RecipientID": guild_id, "RecipientCUID": 0})

    def leave(self):
        return self._call("GuildHandler.LeaveGuild")
