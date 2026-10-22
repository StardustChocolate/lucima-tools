"""Versioned battle roster and authoritative server synchronization.

Native contracts: FantasyStaticData.GetStaticData 0x2170510,
ActivityDBSetting 0x20fc6ac/0x20fc788/0x20fcbf8,
BattleRoleData.SyncEvent 0x21ea678 and BattleStatusContainer 0x225354c.
Property/passive evaluation is a separate layer. Initialization runs the full
callback before filling HP; later trusted events supply current battle values.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import os
from pathlib import Path
import struct
import time

from .game_client import GameError
from .game_data import activity_open, enabled, timestamp


@lru_cache(maxsize=1)
def metadata() -> dict:
    root = Path(os.environ.get("ARK_ASSET_ROOT") or Path(__file__).resolve().parent.parent)
    path = root / "assets" / "abyss_battle_static.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GameError("缺少深渊战斗静态资料，请更新资源") from exc
    required = {"Role", "Skill", "Status", "Fantasy", "AIAction", "Scene", "Activity", "RoleProperty",
                "RoleAwaken", "RoleImprint", "SkillLevel", "Equipment", "EquipmentSet", "Artifact", "PublicVariable"}
    if data.get("schema") != 1 or not isinstance(data.get("tables"), dict) or not required <= data["tables"].keys():
        raise GameError("深渊战斗静态资料版本不受支持")
    return data


def float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def fantasy_config(scene_id: str, state: dict, *, now: float | None = None, tables: dict | None = None) -> dict:
    tables = tables if tables is not None else metadata()["tables"]
    now = time.time() if now is None else now
    for row in tables["Fantasy"].get(scene_id, []):
        if all(activity_open(identifier, state, now=now, tables=tables) for identifier in row["ActivityIDs"]):
            return row
    raise GameError("当前深渊关卡没有开放的怪物配置，请刷新账号状态")


def direct_properties(dynamic: dict) -> dict:
    """Read literal PropertySetting entries; don't infer effects from Fun text."""
    result = {}

    def add_value(key, value):
        if key in result and isinstance(value, (int, float)) and isinstance(result[key], (int, float)):
            result[key] += value
        elif key in result and isinstance(value, (str, list)) and isinstance(result[key], (str, list)):
            old = result[key] if isinstance(result[key], list) else [result[key]]
            result[key] = [*old, *(value if isinstance(value, list) else [value])]
        else:
            result[key] = value

    def add(node):
        if not isinstance(node, dict):
            return
        if node.get("__type__") == "sequence":
            items, separators = node["items"], node["separators"]
            index = 0
            while index < len(items):
                item = items[index]
                if isinstance(item, dict) and item.get("__type__") == "hash":
                    values = [item["value"]]
                    while index + 1 < len(items) and separators[index] == "~" and isinstance(items[index + 1], str):
                        index += 1
                        values.append(items[index])
                    add_value(item["name"], values if len(values) > 1 else values[0])
                else:
                    add(item)
                index += 1
        elif node.get("__type__") == "hash":
            add_value(node["name"], node["value"])

    for node in dynamic.values():
        add(node)
    return result


def status_properties(unit: dict, tables: dict | None = None) -> dict:
    tables = tables if tables is not None else metadata()["tables"]
    result = {}
    for status in unit.get("NowStatusList") or []:
        definition = tables["Status"].get(str(status.get("StaticID") or ""), {})
        for key, value in direct_properties(definition.get("Dynamic", {})).items():
            if key == "Fun":
                continue
            if isinstance(value, (float, int)):
                multiplier = 1
                if enabled(definition.get("IsMergeProperty")):
                    if enabled(definition.get("IsStackMultiplyProp")):
                        multiplier = float32(int(status.get("NowStack", 0)))
                    if enabled(definition.get("IsValueMultiplyProp")):
                        multiplier = float32(multiplier * float32(float(status.get("Value", 0))))
                result[key] = result.get(key, 0) + value * multiplier
            else:
                if key in result:
                    previous = result[key] if isinstance(result[key], list) else [result[key]]
                    result[key] = [*previous, *(value if isinstance(value, list) else [value])]
                else:
                    result[key] = value
    return result


def initialize_units(start_info: dict, fantasy: dict, tables: dict | None = None, *, single_team=False) -> dict[str, dict]:
    tables = tables if tables is not None else metadata()["tables"]
    from .abyss_properties import _skill_level_rows, max_skill_level, refresh_abilities

    scene_id = (start_info.get("SceneData") or {}).get("StaticID")
    scene = tables.get("Scene", {}).get(scene_id, {})
    # These entries are story/support-role account conditions, not Fantasy Bs.
    # Current Abyss scenes have none. Fail explicitly if a future scene adds them.
    if not single_team and (scene.get("RoleStatusInfo1") or scene.get("RoleStatusInfo2")):
        raise GameError("此深渊关卡包含尚未支持的剧情角色初始状态，请更新战斗规则")
    units = {}
    first_team = deepcopy(start_info.get("CampData1") or {})
    if single_team:
        support = ((start_info.get("Support") or {}).get("PlayerRoleData") or {}).get("RoleData")
        roster = first_team.setdefault("PositionRoleMap", {})
        empty = next((str(pos) for pos in range(4) if str(pos) not in roster), None)
        if support and empty is not None:
            roster[empty] = {**deepcopy(support), "_IsSupport": True}
    teams = ([first_team, *({"PositionRoleMap": {}} for _ in fantasy["Waves"][1:])]
             if single_team else start_info["WaveCampDatas"])
    for wave_index, team in enumerate(teams):
        for pos, role in team["PositionRoleMap"].items():
            uid = f"1-{wave_index}-{pos}"
            definition = tables["Role"].get(role["StaticID"], {})
            units[uid] = {**deepcopy(role), "id": uid, "Position": int(pos), "Element": definition.get("Element"),
                          "Job": definition.get("Job"), "NowHP": 1, "NowSP": float(definition.get("InitSP") or 0),
                          "NowStatusList": [], "NowSkillCooldownMap": {}}
        for monster in fantasy["Waves"][wave_index]["Monsters"]:
            pos, sid = int(monster.get("Pos", 1)), monster["M"]
            uid = f"2-{wave_index}-{pos}"
            if uid in units or sid not in tables["Role"]:
                raise GameError(f"深渊怪物配置无效：{sid} / {pos}")
            definition = tables["Role"][sid]
            skills = [{"StaticID": value.split(":", 1)[0], "Level": 1}
                      for value in definition.get("SkillIDs", "").split("~") if value]
            if enabled(direct_properties(definition.get("Dynamic", {})).get("IsMaxSkillLV")):
                for skill in skills:
                    skill["Level"] = max_skill_level(skill["StaticID"], tables)
            units[uid] = {"id": uid, "_id": uid, "StaticID": sid, "Position": pos, "IsBoss": enabled(monster.get("Boss")),
                          "Element": definition.get("Element"), "Job": definition.get("Job"),
                          "LV": int(scene.get("LV") or monster.get("LV") or 1),
                          "Star": int(definition.get("InitStar") or 0), "AwakenLV": 0,
                          "MonsterSetting": deepcopy(monster), "Skills": {"Skills": skills},
                          "NowHP": 1, "NowSP": float(definition.get("InitSP") or 0),
                          "NowStatusList": [], "NowSkillCooldownMap": {}}
            if monster.get("ArtifactID"):
                units[uid]["ArtifactData"] = {"StaticID": monster["ArtifactID"], "LV": int(monster.get("ArtifactLV", 0))}
        wave_units = [u for uid, u in units.items() if uid.split("-")[1] == str(wave_index)]
        if single_team and wave_index == 0:
            _activity_initial_statuses(wave_units, scene, tables)
        buffs = fantasy["Waves"][wave_index].get("Buffs") or {}
        for unit in wave_units:
            for group in ("Bs", "DBs"):
                for buff in buffs.get(group) or []:
                    target = buff.get("Target") or "None"
                    if target not in {"None", "All"} and not (target == "Self" and unit["id"].startswith("1-")) and not (target == "Enemy" and unit["id"].startswith("2-")):
                        continue
                    definition = tables["Status"].get(buff["B"])
                    if definition is None:
                        raise GameError(f"缺少深渊初始状态：{buff['B']}")
                    unit["NowStatusList"].append({"StaticID": buff["B"], "NowStack": 1,
                                                  "NowRound": int(buff.get("Round") or 0),
                                                  "Value": float(definition.get("Value") or 0), "SourceRoleID": unit["id"]})
        init_issues = set()
        refresh_abilities(wave_units, tables, initialize=True, round_role="", issues=init_issues)
        for unit in wave_units:
            definition = tables["Role"][unit["StaticID"]]
            sp = float(definition.get("InitSP") or 0)
            rate_key = {"FightingSpirit": "AddFightSPRate", "Focus": "AddFocusSPRate"}.get(definition.get("SPType"))
            unit["NowSP"] = int(float32(sp * float32(1 + float(unit["_AbilityProperties"].get(rate_key, 0)))))
            skills = (unit.get("Skills") or {}).get("Skills", [])
            for skill in skills[1:]:
                row = tables["Skill"].get(skill["StaticID"], {})
                if enabled(row.get("IsInitCoolDown")):
                    addition = sum(float(direct_properties(level.get("Dynamic", {})).get("CoolDown", 0)) for level in _skill_level_rows(unit, skill, tables))
                    cooldown = int(row.get("CoolDown") or 0) + int(addition)
                    if cooldown > 0:
                        unit["NowSkillCooldownMap"][skill["StaticID"]] = cooldown
            if init_issues:
                unit["_EvaluationIssues"] = sorted(init_issues)
    return units


def _activity_initial_statuses(units: list[dict], scene: dict, tables: dict):
    """SceneRoleStatusInfo.InitBattleRoleData (0x22183cc): team imprint bonus.

    The last non-support featured team member supplies the imprint condition.
    Activity_Main belongs only to that hero; other matching statuses affect
    the whole player team. Mutations apply to combat copies, never warehouse.
    """
    comparisons = {"Eq": lambda a, b: a == b, "Gt": lambda a, b: a > b,
                   "Gte": lambda a, b: a >= b, "Lt": lambda a, b: a < b,
                   "Lte": lambda a, b: a <= b, "Ne": lambda a, b: a != b}
    for field in ("RoleStatusInfo1", "RoleStatusInfo2"):
        setting = scene.get(field) or {}
        if not isinstance(setting, dict):
            raise GameError("活动角色状态配置无效，请更新资源")
        featured = next((unit for unit in reversed(units) if unit["id"].startswith("1-") and not unit.get("_IsSupport")
                         and unit["StaticID"] in setting.get("RoleIDs", [])), None)
        for unit in units:
            if not unit["id"].startswith("1-"):
                continue
            if unit.get("_IsSupport") and unit["StaticID"] in setting.get("RoleIDs", []):
                _maximize_activity_role(unit, tables)
                unit["NowStatusList"].append({"StaticID": "Activity_Main", "NowStack": 1, "NowRound": -1,
                                              "Value": 0, "SourceRoleID": unit["id"]})
                continue
            if featured is None:
                continue
            for condition in setting.get("Conditions", []):
                compare = comparisons.get(condition.get("Comparer", "Eq"))
                if compare is None:
                    raise GameError("活动角色状态条件尚未支持，请更新资源")
                if compare(int(featured.get("ImprintLV") or 0), int(condition.get("Value1") or 0)):
                    sid = condition["ID"]
                    if sid == "Activity_Main":
                        if unit is not featured:
                            continue
                        _maximize_activity_role(unit, tables)
                    definition = tables["Status"].get(sid)
                    if definition is None:
                        raise GameError(f"缺少活动角色状态：{sid}")
                    unit["NowStatusList"].append({"StaticID": sid, "NowStack": 1, "NowRound": -1,
                                                  "Value": float(definition.get("Value") or 0), "SourceRoleID": unit["id"]})


def _maximize_activity_role(unit: dict, tables: dict):
    from .abyss_properties import max_skill_level
    unit.update(Star=6, AwakenLV=6, LV=60)
    for skill in (unit.get("Skills") or {}).get("Skills", []):
        skill["Level"] = max_skill_level(skill["StaticID"], tables)


def sync_activity_support(msg: dict, units: dict, start_info: dict, fantasy: dict):
    """Apply the server's replacement identity before its same-frame role events."""
    replacement = (msg.get("ActionResult") or {}).get("NewSupportRole")
    if not isinstance(replacement, dict):
        return
    uid, role = replacement.get("ID"), replacement.get("RoleData")
    if not isinstance(uid, str) or not uid.startswith("1-") or not isinstance(role, dict):
        raise GameError("助战替换数据不完整，已停止战斗")
    roster = {str(u["Position"]): deepcopy(u) for u in units.values() if u["id"].startswith("1-")}
    pos = str(replacement.get("CampPosition", uid.rsplit("-", 1)[-1]))
    roster[pos] = {**deepcopy(role), "_IsSupport": True}
    info = {**start_info, "CampData1": {"PositionRoleMap": roster}}
    initialized = initialize_units(info, fantasy, single_team=True)
    support = initialized[f"1-0-{pos}"]
    support.update({key: deepcopy(replacement[key]) for key in
                    ("NowHP", "NowSP", "NowRound", "DisplayRound", "RoleTimeInfo", "IsDieOut") if key in replacement})
    support["id"] = uid
    # A replacement created after a wave transition has that wave in its ID.
    units.pop(next((key for key, value in units.items() if key.startswith("1-") and str(value["Position"]) == pos), ""), None)
    units[uid] = support


def sync_units(msg: dict, units: dict[str, dict]) -> None:
    # BattleData.ApplyProcessResult 0x2121b14 applies RoundResult first.
    # A trusted RoleEvent later in the same frame replaces its role's values.
    rr = msg.get("RoundResult") or {}
    for uid, cooldown in (rr.get("SkillCooldownMap") or {}).items():
        if uid in units:
            units[uid]["NowSkillCooldownMap"] = dict(cooldown or {})
    for uid, value in (rr.get("RoleTimeMap") or {}).items():
        if uid in units:
            units[uid]["RoleTimeInfo"] = {"NowTimeLength": value}
            units[uid]["NowTime"] = value
    for event in (msg.get("ActionResult") or {}).get("SkillEventList") or []:
        for item in event.get("RoleEventList") or []:
            if not isinstance(item, dict) or not item.get("TargetRoleID") or item.get("IsCanSyncRole", 0) == 0:
                continue
            uid = str(item["TargetRoleID"])
            current = units.setdefault(uid, {"id": uid})
            # Constructor defaults of a *trusted* BattleRoleEvent. Omission
            # means empty collections, not "leave the old collection alone".
            current["NowStatusList"] = deepcopy(item.get("NowStatusList") or [])
            current["NowSkillCooldownMap"] = dict(item.get("NowSkillCooldownMap") or {})
            for key in ("NowHP", "NowSP", "NowIsDieOut", "NowInjuryRate", "NowTime", "Round", "DisplayRound", "RoundSkillSate"):
                current[key] = item.get(key, 0)
            current["RoleTimeInfo"] = {"NowTimeLength": current["NowTime"]}
            if "MaxHP" in item:
                current["_ServerMaxHP"] = item["MaxHP"]
            for key in ("NowHP", "NowSP", "NowIsDieOut", "NowShieldValue", "NowInjury", "RoleTimeInfo", "NowRound", "MaxHP",
                        "StaticID", "RoleStaticID", "IsBoss", "NowAttack", "Attack", "NowTime"):
                if key in item:
                    current[key] = deepcopy(item[key])
            if not current.get("StaticID") and item.get("RoleStaticID"):
                current["StaticID"] = item["RoleStaticID"]
            # Diagnostic observation, independent of the computed Ability.HP.
            current["_ObservedHPUpperBound"] = max(float(current.get("_ObservedHPUpperBound", 0)),
                                                   float(item.get("LastHP") or 0), float(item.get("NowHP") or 0))
