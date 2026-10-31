"""Compile official tables into the app's existing runtime data contracts."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re

from .build_battle_metadata import build_battle_metadata
from .guild_data_build import build_guild_aid, build_guild_quests, build_guild_store
from .voting import build_catalog as build_voting_catalog

BUILD_VERSION = 2

SLOTS = {"Weapon": "weapon", "Head": "helmet", "Body": "armor", "Necklace": "necklace", "Ring": "ring", "Shoes": "boots"}
SKILL_FIELDS = ("ID", "TargetTypeFun", "SkillType", "TargetType", "TargetCamp", "NoSelf",
                "CoolDown", "Soul", "SkillSoulCost", "SP", "IsInitCoolDown", "TwinSkill", "ReferanceSkillID", "LevelSkillID")
ROLE_FIELDS = ("SkillIDs", "NormalSkillID", "AIs", "HP", "Attack", "Defence", "Speed", "Element", "Job")


def table(source, name, required=("ID",)):
    path = source / "文本/数据表" / f"sd_{name}.txt"
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        rows = csv.DictReader(stream, delimiter="@")
        if not set(required) <= set(rows.fieldnames or []):
            raise ValueError(f"数据表 {name} 缺少必需字段")
        result = list(rows)
    if any(None in row or any(row.get(key) is None or "\ufffd" in row[key] for key in required) for row in result):
        raise ValueError(f"数据表 {name} 格式无效")
    return result


def keyed(source, name, required=("ID",)):
    rows = table(source, name, required)
    result = {row["ID"]: row for row in rows}
    if len(result) != len(rows) or "" in result or not result:
        raise ValueError(f"数据表 {name} 的 ID 无效或重复")
    return result


def plain(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", value)).strip()


def compile_data(source: Path, version: str, previous):
    """No images, processed Ark2 files, or external project are required."""
    loc = json.loads((source / "文本/本地化/loc_CHS_FINAL.json").read_text(encoding="utf-8"))
    hashes = {path.stem[3:]: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (source / "文本/数据表").glob("sd_*.txt")}
    provenance = {"version": "official-" + version, "table_sha256": hashes}
    battle, evidence = build_battle_metadata(source, provenance)
    skills = keyed(source, "Skill", SKILL_FIELDS)
    roles = keyed(source, "Role", ("ID", "IsHero", "SkillIDs", "AIs", "Element", "Job"))
    items = keyed(source, "Item", ("ID", "Name", "ItemRank", "ItemType", "Tags"))
    equipment = keyed(source, "Equipment", ("ID", "Part", "EquipLV"))
    commodities = table(source, "Commodity", ("ID", "Store", "ResetCountType", "LimitCount"))
    hero_ids = {key for key, row in roles.items() if row["IsHero"] == "1"}
    if not hero_ids or any(not re.fullmatch(r"H\d{3}", key) for key in hero_ids):
        raise ValueError("正式角色集合无效")
    compact_roles = {}
    for key, row in roles.items():
        ids = list(filter(None, row["SkillIDs"].split("~")))
        compact_roles[key] = {field: row.get(field, "") for field in ROLE_FIELDS}
        compact_roles[key].update(SkillIDs=ids, NormalSkillID=ids[0].split(":", 1)[0] if ids else "")
    static = {"version": provenance["version"], "source": provenance,
              "skills": {key: {field: row[field] for field in SKILL_FIELDS} for key, row in skills.items()},
              "roles": compact_roles,
              "rolesByStatic": {key: {field: compact_roles[key][field] for field in ("NormalSkillID", "SkillIDs", "AIs", "Element", "Job")}
                                for key in sorted(hero_ids)}}
    names = dict(previous("backend/item_names.json"))
    ranks = dict(previous("backend/item_rank.json"))
    categories = {}
    for key, row in items.items():
        name = loc.get(row["Name"])
        if isinstance(name, str) and plain(name):
            names[key] = plain(name)
        rank = int(row["ItemRank"] or 0)
        if not 0 <= rank <= 5:
            raise ValueError(f"物品 {key} 稀有度无效")
        ranks[key] = rank
        kind = row["ItemType"]
        if kind.endswith("Charm"):
            category = "enhancement"
        elif kind == "Rune":
            category = "element"
        elif kind in {"Crafting", "ReforgeMat"} and key.startswith("CR"):
            category = "crystal"
        elif kind == "ActivityCurrency" and key.startswith("AH"):
            category = "activity"
        elif kind == "Catalyst":
            category = "catalyst"
        else:
            category = "wealth" if key.isdigit() else "other"
        categories[key] = category
    equips = {key: dict(value) for key, value in previous("backend/equip_ref.json").items()}
    for key, row in equipment.items():
        if row["Part"] not in SLOTS or int(row["EquipLV"] or 0) <= 0:
            raise ValueError(f"装备 {key} 的部位或等级无效")
        old = equips.get(key, {})
        equips[key] = {**old, "level": int(row["EquipLV"]), "slot": SLOTS[row["Part"]],
                       "name": plain(loc.get(items.get(key, {}).get("Name"), "")) or old.get("name", ""),
                       "img": old.get("img", "")}
    commodity_rules = {}
    for row in commodities:
        key = row["ID"]
        reset = row["ResetCountType"] or None
        if reset not in {None, "Day", "Week", "Month", "Hour"}:
            raise ValueError(f"商品 {key} 的重置周期不受支持")
        limit = int(row["LimitCount"]) if row["LimitCount"] else None
        rule = {"store": row["Store"], "resetType": reset,
                "limit": limit if limit is None or limit >= 0 else None}
        if row["Store"] == "CaptainShop":
            if any(row.get(field) for field in ("PriceItemID2", "Price2", "Cost", "Condition", "Discount")):
                raise ValueError(f"团长商品 {key} 的购买规则不受支持")
            rule.update(price=int(row["Price"]), currency=row["PriceItemID"],
                        name=plain(loc.get(row["Name"], "")))
        if key in commodity_rules and commodity_rules[key] != rule:
            raise ValueError(f"商品 {key} 的限购规则重复且不一致")
        commodity_rules[key] = rule
    activities = dict(previous("backend/activity_names.json"))
    prefix = "T_Chapter_Name_Branch"
    for key, value in loc.items():
        if key.startswith(prefix) and isinstance(value, str):
            family = key[len(prefix):]
            activities["BH" + family[1:] if family.startswith("H") else family] = plain(value)
    config = {"schema": 1, "tables": {key: battle["tables"][key] for key in ("Activity", "PublicVariable")}}
    config['voting'] = build_voting_catalog(
        table(source, 'Vote', ('ActivityID', 'VoteItemID', 'VoteJsonString')),
        table(source, 'Quest'), loc)
    if int(config["tables"]["PublicVariable"].get("GuildWarLessLV", {}).get("VALUE", 0)) <= 0:
        raise ValueError("佣兵团公共参数无效")
    result = {"assets/abyss_battle_static.json": battle, "assets/ark2_skill_static.json": static,
              "assets/game_config.json": config, "assets/localization/loc_CHS_FINAL.json": loc,
              "assets/role_elements.json": {key: row["Element"] for key, row in roles.items() if row["Element"]},
              "assets/role_jobs.json": {key: row["Job"] for key, row in roles.items() if row["Job"]},
              "assets/item_categories.json": categories,
              "backend/item_names.json": names, "backend/item_rank.json": ranks,
              "backend/equip_ref.json": equips, "backend/activity_names.json": activities,
              "backend/commodity_catalog.json": {"schema": 1, "source": "sd_Commodity.txt", "commodities": commodity_rules},
              "assets/guild_store.json": build_guild_store(source),
              "assets/guild_aid.json": build_guild_aid(source),
              "assets/guild_quests.json": build_guild_quests(source)}
    return result, {"heroes": len(hero_ids), "tables": len(hashes), "scenes": len(battle["tables"]["Scene"]),
                    "unparsedFields": len(evidence["unparsed_fields"])}
