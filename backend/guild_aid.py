"""Guild aid decisions from confirmed guild records and the extracted item catalog."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import os
from pathlib import Path
import time

from .game_client import GameError
from .guild import checked_in_today, has_guild, object_id, _nonnegative_int, _timestamp

AID_REWARD_LIMIT = 200  # GuildAidItemInfoUI.SupportGuildAid, client RVA 0x2770b68.
AID_POLL_MS = 60 * 60 * 1000
DAY_MS = 86_400_000


@lru_cache(maxsize=1)
def aid_catalog():
    root = Path(os.environ.get("ARK_ASSET_ROOT") or Path(__file__).resolve().parents[1])
    data = json.loads((root / "assets/guild_aid.json").read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not data.get("rules") or not data.get("items"):
        raise GameError("佣兵团援助清单无效")
    return data


def aid_options(value=None):
    value = value if isinstance(value, dict) else {}
    item = value.get("item")
    return {"request": bool(value.get("request", True)),
            "item": item if isinstance(item, str) and item in aid_catalog()["items"] else "",
            "hunt": bool(value.get("hunt", True)), "element": bool(value.get("element", True)),
            "catalyst": bool(value.get("catalyst", False))}


class GuildAidService:
    def __init__(self, guild):
        self.guild = guild
        self.client = guild.client
        self.runtime = guild.runtime

    def snapshot(self):
        state = self.client.account_state or {}
        own = state.get("PlayerGuildInfo") or {}
        joined = has_guild(state)
        today = checked_in_today(own, self.guild.now_ms())
        last = _timestamp(own.get("LastRequestGuildAidTime"))
        return {"requested": joined and last is not None and last // DAY_MS == self.guild.now_ms() // DAY_MS,
                "requestKnown": last is not None,
                "reward": _nonnegative_int(own.get("DaySupportGuildAidRewardCount")) if joined and today else None,
                "limit": AID_REWARD_LIMIT, "running": self.runtime.get("aiding"),
                "error": self.runtime.get("aidError"), "nextPollAt": self.runtime.get("aidNextPollAtMs")}

    def note_synced(self):
        """Accept an omitted empty list, but require authoritative member state."""
        state = self.client.account_state or {}
        guild = state.get("GuildData") or {}
        cuid = (state.get("Info") or {}).get("CUID")
        own = next((member.get("PlayerGuildInfo") for member in guild.get("MemberList") or []
                    if isinstance(member, dict) and cuid is not None
                    and (member.get("PlayerInfo") or {}).get("CUID") == cuid), None)
        # GuildData's client constructor initializes this list empty; the server
        # may omit it when no requests exist. Explicit malformed values still fail.
        valid = (isinstance(guild.get("GuildAidItemInfoList", []), list) and isinstance(own, dict)
                 and _timestamp(own.get("LastRequestGuildAidTime")) is not None
                 and _nonnegative_int(own.get("DaySupportGuildAidRewardCount")) is not None)
        self.runtime["aidSynced"] = valid
        if valid:
            self.runtime["aidNextPollAtMs"] = int(time.time() * 1000) + AID_POLL_MS
            self.runtime.pop("aidRetryAtMs", None)
            self.runtime.pop("aidError", None)
        return valid

    def request_due(self, options):
        status = self.snapshot()
        return (self.runtime.get("aidSynced") and options["request"] and bool(options["item"])
                and status["requestKnown"] and not status["requested"])

    def candidates(self, options):
        status = self.snapshot()
        state = self.client.account_state or {}
        cuid = (state.get("Info") or {}).get("CUID")
        if (not self.runtime.get("aidSynced") or cuid is None or status["reward"] is None
                or status["reward"] >= AID_REWARD_LIMIT):
            return []
        catalog = aid_catalog()
        balances = {str(item.get("StaticID")): _nonnegative_int(item.get("Count")) or 0
                    for item in (state.get("ItemContainer") or {}).get("Items") or [] if isinstance(item, dict)}
        uncertain = self.runtime.get("aidUncertain") or {}
        # A timed-out spend may have committed without returning NowItem. Keep
        # its cost unavailable until the next login restores absolute balances.
        for pending in uncertain.values():
            if pending.get("reserve"):
                item = pending["item"]
                balances[item] = max(0, balances.get(item, 0) - pending["cost"])
        gid = object_id((state.get("PlayerGuildInfo") or {}).get("GID"))
        result = []
        for record in (state.get("GuildData") or {}).get("GuildAidItemInfoList") or []:
            if not isinstance(record, dict):
                continue
            identifier = object_id(record.get("_id"))
            item = catalog["items"].get(record.get("ItemID"))
            if not item or not identifier or identifier in uncertain or object_id(record.get("GID")) != gid:
                continue
            rule = catalog["rules"][item["rule"]]
            supporters = record.get("SupporterList", [])
            requester = record.get("Requester") or {}
            count = _nonnegative_int(record.get("NowCount"))
            if (not options.get(rule["group"]) or not isinstance(requester, dict)
                    or requester.get("CUID") is None or requester.get("CUID") == cuid
                    or not isinstance(supporters, list) or cuid in supporters
                    or count is None or count + rule["cost"] > rule["max"]
                    or status["reward"] + rule["reward"] > AID_REWARD_LIMIT
                    or balances.get(record["ItemID"], 0) < rule["cost"]):
                continue
            result.append(record)
        return result

    def next_delay(self, options):
        if not has_guild(self.client.account_state) or not any((
                options["request"] and options["item"], options["hunt"], options["element"], options["catalyst"])):
            return None
        retry = self.runtime.get("aidRetryAtMs")
        if retry is not None:
            return max(0.0, (retry - time.time() * 1000) / 1000)
        if not self.runtime.get("aidSynced"):
            return 0.0
        status = self.snapshot()
        now = self.guild.now_ms()
        # Recheck at the game-day boundary even when both daily actions finished.
        boundary = (DAY_MS - now % DAY_MS) / 1000
        if not checked_in_today((self.client.account_state or {}).get("PlayerGuildInfo"), now):
            return 0.0
        if self.request_due(options) or self.candidates(options):
            return 0.0
        complete = (not options["request"] or not options["item"] or status["requested"]) and (
            not (options["hunt"] or options["element"] or options["catalyst"]) or
            (status["reward"] is not None and status["reward"] >= AID_REWARD_LIMIT))
        if complete:
            return boundary
        poll = max(0.0, (self.runtime.get("aidNextPollAtMs", 0) - time.time() * 1000) / 1000)
        return min(boundary, poll)

    def needs_query(self):
        return (not self.runtime.get("aidSynced") or self.runtime.get("aidRetryAtMs") is not None
                or time.time() * 1000 >= self.runtime.get("aidNextPollAtMs", 0)
                or not checked_in_today((self.client.account_state or {}).get("PlayerGuildInfo"), self.guild.now_ms()))

    def _replace(self, record):
        guild = self.client.account_state["GuildData"]
        identifier = object_id(record.get("_id"))
        rows = guild.setdefault("GuildAidItemInfoList", [])
        rows[:] = [row for row in rows if isinstance(row, dict)
                   and object_id(row.get("_id")) != identifier] + [deepcopy(record)]

    def request(self, options):
        if not self.request_due(options):
            return False
        result = self.guild._call("GuildHandler.RequestGuildAid", {"ItemID": options["item"]})
        record = result.get("GuildAidItemInfo")
        state = self.client.account_state
        if (not isinstance(record, dict) or not object_id(record.get("_id"))
                or record.get("ItemID") != options["item"]
                or (record.get("Requester") or {}).get("CUID") != state["Info"].get("CUID")
                or object_id(record.get("GID")) != object_id(state["PlayerGuildInfo"].get("GID"))):
            raise GameError("求助响应未确认，等待同步后检查")
        self._replace(record)
        # The official success callback records NowTime locally; the reply only
        # contains GuildAidItemInfo and Utc, not PlayerGuildInfo.
        state["PlayerGuildInfo"]["LastRequestGuildAidTime"] = {"$date": self.guild.now_ms()}
        return True

    def support(self, record, options):
        identifier = object_id(record.get("_id"))
        if not any(object_id(row.get("_id")) == identifier for row in self.candidates(options)):
            return 0
        catalog = aid_catalog()
        item = record["ItemID"]
        rule = catalog["rules"][catalog["items"][item]["rule"]]
        reward_before = self.snapshot()["reward"]
        merged_cost = False
        try:
            response = self.guild._call("GuildHandler.SupportGuildAid", {"GuildAidItemInfoID": identifier})
            costs = response.get("CostItems") or []
            merged_cost = any(isinstance(cost, dict) and isinstance(cost.get("NowItem"), dict)
                              and cost["NowItem"].get("StaticID") == item
                              and _nonnegative_int(cost["NowItem"].get("Count")) is not None for cost in costs)
            updated = response.get("GuildAidItemInfo")
            cuid = self.client.account_state["Info"].get("CUID")
            if (not isinstance(updated, dict) or object_id(updated.get("_id")) != identifier
                    or updated.get("ItemID") != item or cuid not in (updated.get("SupporterList") or [])
                    or object_id(updated.get("GID")) != object_id(record.get("GID"))
                    or (updated.get("Requester") or {}).get("CUID") != record["Requester"]["CUID"]
                    or _nonnegative_int(updated.get("NowCount")) is None
                    or updated["NowCount"] < record["NowCount"] + rule["cost"] or not merged_cost):
                raise GameError("援助响应未确认，等待同步后检查")
            self._replace(updated)
            self.client.account_state["PlayerGuildInfo"]["DaySupportGuildAidRewardCount"] = reward_before + rule["reward"]
            return rule["reward"]
        except Exception:
            self.runtime.setdefault("aidUncertain", {})[identifier] = {
                "item": item, "cost": rule["cost"], "reserve": not merged_cost}
            raise
