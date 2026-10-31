"""Shared guild data builders; all values come from the official tables."""

from __future__ import annotations

import csv

import hashlib

import json

from pathlib import Path

GROUPS = {"CatalystRare": "catalyst", "Rune1": "element", "Rune2": "element", "Mat1": "hunt", "Mat2": "hunt", "Mat3": "hunt", "Mat4": "hunt"}

def build_guild_store(source: Path) -> dict:
    def table(name):
        with (source / '文本/数据表' / f'sd_{name}.txt').open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream, delimiter='@'))

    items = {row['ID']: row for row in table('Item')}
    loc = json.loads((source / '文本/本地化/loc_CHS_FINAL.json').read_text(encoding='utf-8-sig'))
    goods = {}
    activities = {row['ID']: row for row in table('Activity')}
    activity_rules = {}
    for row in table('Commodity'):
        if row['Store'] != 'MemberShop':
            continue
        identifier, item, currency = row['ID'], row['ItemID'], row['PriceItemID']
        reset, limit = row['ResetCountType'] or None, int(row['LimitCount']) if row['LimitCount'] else None
        # New or more complex pricing must be implemented deliberately, never
        # silently purchased using only the first currency or a partial rule.
        unsupported = ('PriceItemID2', 'Price2', 'Cost', 'Condition', 'AccountLv', 'VIPLV',
                       'LifeTime', 'CDTime', 'IsRandomOnce', 'Discount', 'ActivityID')
        if (any(row.get(key) for key in unsupported) or reset not in (None, 'Week', 'Month')
                or not item or currency not in ('29', '30') or int(row['Price']) <= 0
                or (reset and (limit is None or limit <= 0))):
            raise ValueError(f'Unsupported member-shop rule: {identifier}')
        name = loc.get(items[item]['Name'])
        if not isinstance(name, str) or not name:
            raise ValueError(f'Missing member-shop name: {item}')
        for key in filter(None, row['ActivityIDs'].split('~')):
            if key not in activities:
                raise ValueError(f'Missing member-shop activity: {key}')
            activity_rules[key] = {'IsUnlimitTime': activities[key].get('IsUnlimitTime', '')}
        goods[identifier] = {'item': item, 'name': name, 'amount': int(row['ItemCount'] or 1),
                             'currency': currency, 'price': int(row['Price']), 'reset': reset,
                             'limit': limit, 'activities': list(filter(None, row['ActivityIDs'].split('~')))}
    if not goods:
        raise ValueError('Empty member shop')
    return {'schema': 1, 'source': {name: hashlib.sha256(
        (source / '文本/数据表' / f'sd_{name}.txt').read_bytes()).hexdigest()
        for name in ('Commodity', 'Item', 'Activity')}, 'goods': goods, 'activities': activity_rules}

def build_guild_aid(source: Path) -> dict:
    def table(name, fields):
        path = source / "文本/数据表" / f"sd_{name}.txt"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="@")
            if not set(fields).issubset(reader.fieldnames or []):
                raise ValueError(f"Invalid {name} table header")
            rows = list(reader)
        if len({r["ID"] for r in rows}) != len(rows):
            raise ValueError(f"Duplicate IDs in {name}")
        return rows

    rules = {}
    for row in table("GuildAidItem", ("ID", "MaxCount", "PerSupportCount", "RewardCount")):
        if row["ID"] not in GROUPS:
            raise ValueError(f"Unknown guild aid category: {row['ID']}")
        values = {key: int(row[field]) for key, field in
                  (("max", "MaxCount"), ("cost", "PerSupportCount"), ("reward", "RewardCount"))}
        if any(value <= 0 for value in values.values()) or values["max"] % values["cost"]:
            raise ValueError(f"Invalid guild aid quantities: {row['ID']}")
        rules[row["ID"]] = {"group": GROUPS[row["ID"]], **values}
    if not rules:
        raise ValueError("Empty guild aid rules")
    loc = json.loads((source / "文本/本地化/loc_CHS_FINAL.json").read_text(encoding="utf-8-sig"))
    items = {}
    for row in table("Item", ("ID", "Name", "Tags")):
        tags = set(row["Tags"].split("~")) & rules.keys()
        if not tags:
            continue
        if len(tags) != 1:
            raise ValueError(f"Ambiguous guild aid tags: {row['ID']}")
        name = loc.get(row["Name"])
        if not isinstance(name, str) or not name.strip() or "\ufffd" in name:
            raise ValueError(f"Missing guild aid item name: {row['ID']}")
        items[row["ID"]] = {"name": name, "rule": next(iter(tags))}
    if not items:
        raise ValueError("Empty guild aid item catalog")
    return {"schema": 1, "source": {name: hashlib.sha256(
                (source / "文本/数据表" / f"sd_{name}.txt").read_bytes()).hexdigest()
            for name in ("GuildAidItem", "Item")}, "rules": rules, "items": dict(sorted(items.items()))}

def build_guild_quests(source: Path) -> dict:
    def table(name, fields):
        with (source / "文本/数据表" / f"sd_{name}.txt").open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="@")
            if not set(fields).issubset(reader.fieldnames or []):
                raise ValueError(f"Invalid {name} table header")
            return list(reader)

    loc = json.loads((source / "文本/本地化/loc_CHS_FINAL.json").read_text(encoding="utf-8-sig"))
    items = {row["ID"]: row for row in table("Item", ("ID", "Name"))}
    drops = table("Drop", ("DropID", "TagString", "MinItemCount", "MaxItemCount", "Rate"))
    personal, tiers = {}, {}
    for row in table("Quest", ("ID", "Name", "Tags", "ConditionCount", "Drops", "GuildDrops")):
        tags = row["Tags"].split("~")
        if row.get("Group") != "Guild" or not {"GuildQuest", "GuildPoint"}.intersection(tags):
            continue
        target = int(row["ConditionCount"])
        if target <= 0:
            raise ValueError(f"Invalid guild quest target: {row['ID']}")
        if "GuildQuest" in tags:
            name = loc.get(row["Name"])
            if not isinstance(name, str) or not name.strip() or "\ufffd" in name:
                raise ValueError(f"Missing guild quest name: {row['ID']}")
            points = 0
            for drop in drops:
                if drop["DropID"] != row["GuildDrops"] or drop["TagString"] != "82":
                    continue
                if (float(drop["Rate"]) != 1 or drop["MinItemCount"] != drop["MaxItemCount"]
                        or drop.get("Group") or drop.get("DropFun")):
                    raise ValueError(f"Unsupported guild quest points: {row['ID']}")
                count = int(drop["MinItemCount"])
                if count <= 0:
                    raise ValueError(f"Invalid guild quest points: {row['ID']}")
                points += count
            personal[row["ID"]] = {"name": name, "target": target, "points": points}
        else:
            rewards = []
            for drop in drops:
                if drop["DropID"] != row["Drops"]:
                    continue
                item = drop["TagString"]
                if (item not in items or float(drop["Rate"]) != 1
                        or drop["MinItemCount"] != drop["MaxItemCount"]):
                    raise ValueError(f"Unsupported guild tier drop: {row['ID']}")
                name = loc.get(items[item]["Name"])
                count = int(drop["MinItemCount"])
                if not isinstance(name, str) or count <= 0:
                    raise ValueError(f"Invalid guild tier reward: {row['ID']}")
                rewards.append({"item": item, "name": name, "count": count})
            if not rewards:
                raise ValueError(f"Missing guild tier reward: {row['ID']}")
            tiers[row["ID"]] = {"target": target, "rewards": rewards}
    if not personal or not tiers:
        raise ValueError("Empty guild weekly catalog")
    return {"schema": 1, "source": {name: hashlib.sha256(
                (source / "文本/数据表" / f"sd_{name}.txt").read_bytes()).hexdigest()
            for name in ("Quest", "Drop", "Item")}, "personal": personal, "tiers": tiers}
