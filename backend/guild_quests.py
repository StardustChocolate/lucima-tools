"""Guild weekly reward eligibility from server-selected quests and member saves."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import time

from .game_client import GameError
from .guild import has_guild, object_id, _nonnegative_int

WEEKLY_POLL_MS = 3_600_000
DAY_MS = 86_400_000


@lru_cache(maxsize=1)
def weekly_catalog():
    root = Path(os.environ.get("ARK_ASSET_ROOT") or Path(__file__).resolve().parents[1])
    data = json.loads((root / "assets/guild_quests.json").read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not data.get("personal") or not data.get("tiers"):
        raise GameError("佣兵团周任务清单无效")
    return data


class GuildWeeklyService:
    def __init__(self, guild):
        self.guild, self.client, self.runtime = guild, guild.client, guild.runtime

    def snapshot(self):
        state = self.client.account_state or {}
        joined = has_guild(state)
        guild = state.get("GuildData") or {}
        record = guild.get("Record") or {}
        week = _nonnegative_int(record.get("LastResetQuestWeek")) if joined else None
        personal_known = week is not None and self.runtime.get("weeklyPersonalWeek") == week
        catalog = weekly_catalog()
        quests = {row.get("StaticID"): row for row in (state.get("QuestContainer") or {}).get("Quests") or []
                  if isinstance(row, dict)}
        pending = self.runtime.get("weeklyUncertain") or []
        personal = []
        ids = record.get("GuildQuestIDList") or [] if joined else []
        for identifier in dict.fromkeys(item for item in ids if isinstance(item, str)):
            meta = catalog["personal"].get(identifier) or {}
            quest = quests.get(identifier) or {} if personal_known else {}
            count = _nonnegative_int(quest.get("Count"))
            target = meta.get("target")
            rewarded = quest.get("IsRewarded") is True
            uncertain = identifier in pending and not rewarded
            ready = (personal_known and target is not None and count is not None and count >= target
                     and quest.get("IsFinish") is True and quest.get("IsRewarded") is False and not uncertain)
            personal.append({"id": identifier, "name": meta.get("name", identifier), "count": count,
                             "target": target, "state": "claimed" if rewarded else "uncertain" if uncertain
                             else "ready" if ready else "progress" if target and count is not None else "unknown"})
        own = state.get("PlayerGuildInfo") or {}
        save = own.get("PlayerGuildQuestSave") or {}
        save_week = _nonnegative_int(save.get("LastRewardQuestWeek"))
        rewarded = save.get("RewardedGuildQuestList", [])
        rewards_known = (week is not None and save_week is not None and save_week <= week
                         and isinstance(rewarded, list) and all(isinstance(item, str) for item in rewarded))
        claimed = set(rewarded) if rewards_known and save_week == week else set()
        rows = {row.get("StaticID"): row for row in guild.get("GuildQuestList") or [] if isinstance(row, dict)}
        gid = object_id(own.get("GID"))
        tiers, scores = [], []
        for identifier, meta in sorted(catalog["tiers"].items(), key=lambda pair: pair[1]["target"]):
            row = rows.get(identifier) or {}
            quest = row.get("QuestData") or {}
            current = (joined and week is not None and row.get("Week") == week
                       and object_id(row.get("GID")) == gid)
            count = _nonnegative_int(quest.get("Count")) if current else None
            if count is not None:
                scores.append(count)
            valid = rewards_known and current
            ready = valid and count is not None and count >= meta["target"] and quest.get("IsFinish") is True
            tiers.append({"id": identifier, **meta, "state": "claimed" if identifier in claimed
                          else "ready" if ready else "progress" if valid else "unknown"})
        return {"week": week, "personalKnown": personal_known, "rewardsKnown": rewards_known,
                "personal": personal if joined else [], "tiers": tiers if joined else [],
                # Completed tiers can stop counting; the client uses the highest
                # tier counter, never the sum of all tier counters.
                # WeeklyAssistanceScore belongs to contribution, not quest points.
                "score": max(scores, default=None),
                "personalClaimed": sum(row["state"] == "claimed" for row in personal),
                "tiersClaimed": sum(row["state"] == "claimed" for row in tiers) if joined else 0,
                "running": self.runtime.get("weeklyRunning"), "error": self.runtime.get("weeklyError")}

    def note_synced(self):
        status = self.snapshot()
        guild = (self.client.account_state or {}).get("GuildData") or {}
        record = guild.get("Record") or {}
        valid = (status["rewardsKnown"] and isinstance(record.get("GuildQuestIDList"), list)
                 and isinstance(guild.get("GuildQuestList"), list))
        self.runtime["weeklySynced"] = valid
        if valid:
            self.runtime["weeklyNextPollAtMs"] = int(time.time() * 1000) + WEEKLY_POLL_MS
            self.runtime["weeklySyncedDay"] = self.guild.now_ms() // DAY_MS
            self.runtime.pop("weeklyRetryAtMs", None)
            self.runtime.pop("weeklyError", None)
        return valid

    def needs_query(self):
        return (not self.runtime.get("weeklySynced") or self.runtime.get("weeklyRetryAtMs") is not None
                or time.time() * 1000 >= self.runtime.get("weeklyNextPollAtMs", 0)
                or self.runtime.get("weeklySyncedDay") != self.guild.now_ms() // DAY_MS)

    def next_delay(self):
        if self.runtime.get("weeklyScoreJob") or not has_guild(self.client.account_state):
            return None
        retry = self.runtime.get("weeklyRetryAtMs")
        if retry is not None:
            return max(0.0, (retry - time.time() * 1000) / 1000)
        if self.needs_query():
            return 0.0
        status = self.snapshot()
        if any(row["state"] == "ready" for row in status["personal"] + status["tiers"]):
            return 0.0
        return min(max(0.0, (self.runtime["weeklyNextPollAtMs"] - time.time() * 1000) / 1000),
                   (DAY_MS - self.guild.now_ms() % DAY_MS) / 1000)

    def claim_personal(self, identifier):
        status = self.snapshot()
        if not any(row["id"] == identifier and row["state"] == "ready" for row in status["personal"]):
            return False
        # QueryGuildDataForSync cannot confirm an ambiguous personal reward.
        # Keep it blocked until a fresh personal quest update / login confirms it.
        pending = self.runtime.setdefault("weeklyUncertain", [])
        if identifier not in pending:
            pending.append(identifier)
        self.guild.claim_quest(identifier)
        after = self.snapshot()
        if (after["week"] != status["week"] or not any(
                row["id"] == identifier and row["state"] == "claimed" for row in after["personal"])):
            raise GameError("个人周任务领奖状态未确认，等待登录快照确认")
        pending.remove(identifier)
        return True

    def claim_tiers(self):
        before = self.snapshot()
        ready = [row["id"] for row in before["tiers"] if row["state"] == "ready"]
        if not ready:
            return 0
        self.guild._call("QuestHandler.RewardGuildQuest")
        after = self.snapshot()
        claimed = {row["id"] for row in after["tiers"] if row["state"] == "claimed"}
        if after["week"] != before["week"] or not set(ready).issubset(claimed):
            raise GameError("全团积分奖励领取状态未确认，将查询后检查")
        return len(ready)
