"""Compile decision inputs without localization, images or native evidence.

Repeated AI/Fantasy rows retain source order. Level tables use composite keys.
This is part of sync_game_metadata, not a separate one-off resource refresh.
"""
from __future__ import annotations

import hashlib
from collections import Counter
import json
from pathlib import Path

try:
    from .game_table_dsl import DSLParser, DSLParseError
except ImportError:
    from game_table_dsl import DSLParser, DSLParseError


BATTLE_PATH = Path("assets/abyss_battle_static.json")
STATS = ("HP", "Attack", "Defence", "Speed", "CriticalRate", "CriticalDamageRate",
         "EffectHitRate", "ResistanceRate", "PinchRate")
TABLE_FIELDS = {
    "Role": ("ID", "IsHero", "SkillIDs", "AIs", "Element", "Job", "RolePropertyID",
             "InitStar", "InitSP", "SPType", "TeamImprint", "SelfImprint", "ImprintPositions", *STATS),
    "Skill": ("ID", "TargetTypeFun", "SkillType", "TargetType", "TargetCamp", "NoSelf",
              "CoolDown", "Soul", "SkillSoulCost", "SP", "IsInitCoolDown", "TwinSkill", "ReferanceSkillID", "LevelSkillID"),
    "Status": ("ID", "Group", "IsBuffType", "MaxStack", "CanMerge", "IsMergeProperty",
               "IsStackMultiplyProp", "IsValueMultiplyProp", "DeadResident", "DeadAdd", "Value"),
    "Fantasy": ("ID", "WaveMonsterJson1", "WaveBuffJson1", "WaveMonsterJson2", "WaveBuffJson2", "ActivityIDs"),
    "AIAction": ("ID", "Priority", "Condition1", "Condition2", "Condition3",
                 "TargetSetting1", "TargetSetting2", "TargetSetting3", "TargetSetting4", "TargetSetting5", "SkillIndex"),
    "Scene": ("ID", "Chapter", "Name", "PreScene", "LV", "Energy", "EnterCost", "ActivityIDs", "ServerBattle", "TurnRole",
              "RoleStatusInfo1", "RoleStatusInfo2", "MyCampTeam", "WaveInfoJsonString", "WaveInfo2JsonString", "WaveInfo3JsonString"),
    "Activity": ("ID", "IsUnlimitTime"),
    "RoleProperty": ("ID", "LV", *STATS),
    "RoleAwaken": ("RoleID", "LV", "EnhanceSkill", "HPValue", "AttackValue", "HPRate", "AttackRate",
                   "DefenceRate", "SpeedValue", "CriticalRate", "CriticalDamageRate", "PinchRate", "EffectHitRate", "ResistanceRate"),
    "RoleImprint": ("ID",),
    "SkillLevel": ("SkillID", "LV"),
    "Equipment": ("ID", "Part", "EquipLV", "Sets"),
    "EquipmentSet": ("ID", "Count", "HPRate", "AttackRate", "DefenceRate", "SpeedRate", "CriticalRate",
                     "CriticalDamageRate", "PinchRate", "CounterRate", "EffectHitRate", "ResistanceRate", "LifeStealRate"),
    "Artifact": ("ID", "Job", "Star", "IsFunSingle", "Base.AttackValue", "Base.HPValue", "Max.AttackValue", "Max.HPValue"),
    "PublicVariable": ("ID", "VALUE", "SValue"),
}
LEVEL_KEYS = {"RoleProperty": "ID", "RoleAwaken": "RoleID", "SkillLevel": "SkillID"}


def read_rows(source: Path, name: str):
    path = source / "文本" / "数据表" / f"sd_{name}.txt"
    lines = path.read_bytes().decode("utf-8-sig", "replace").splitlines()
    headers = lines[0].split("@")
    if len(headers) != len(set(headers)) or not set(TABLE_FIELDS[name]) <= set(headers):
        raise ValueError(f"Missing or duplicate battle columns: {name}")
    for number, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        values = line.split("@")
        if len(values) == len(headers) + 1 and not values[-1]:
            values.pop()
        if len(values) != len(headers):
            raise ValueError(f"Invalid battle table row: {name}:{number}")
        row = dict(zip(headers, values))
        # No translated descriptions, names, art, sound or animation metadata.
        fields = (*TABLE_FIELDS[name], *(key for key in headers if "DynamicField" in key))
        consumed = {key: row[key] for key in fields if row[key] != ""}
        if any("\ufffd" in value for value in consumed.values()):
            raise ValueError(f"Invalid consumed battle text: {name}:{number}")
        yield number, consumed


def build_battle_metadata(source: Path, provenance: dict) -> tuple[dict, dict]:
    tables, counts, unresolved, hashes = {}, {}, [], {}
    extraction_path = source / "文本" / "文本提取报告.json"
    extraction = json.loads(extraction_path.read_text(encoding="utf-8-sig")) if extraction_path.exists() else {}
    extraction_hashes = {row["name"]: row.get("sha256") for row in extraction.get("static_table_details", [])}
    census = {"CheckFun": Counter(), "Property": Counter(), "TargetType": Counter()}

    def parse(value, name, line, field):
        try:
            result = DSLParser(value).parse()
        except DSLParseError as exc:
            # Preserve unimplemented property input; executable selectors must parse.
            if name in {"Fantasy", "AIAction", "Scene"}:
                raise ValueError(f"Invalid battle DSL {name}:{line}:{field}: {exc}") from exc
            unresolved.append({"table": name, "line": line, "field": field})
            return {"__unparsed__": value}
        walk(result)
        return result

    def walk(value):
        if isinstance(value, dict):
            for key in census:
                if isinstance(value.get(key), str):
                    census[key][value[key]] += 1
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for name in TABLE_FIELDS:
        rows = list(read_rows(source, name))
        hashes[name] = hashlib.sha256((source / "文本" / "数据表" / f"sd_{name}.txt").read_bytes()).hexdigest()
        expected_hash = provenance["table_sha256"].get(name)
        if expected_hash and expected_hash != hashes[name]:
            raise ValueError(f"Battle source hash differs: {name}")
        if extraction and extraction_hashes.get(name) != hashes[name]:
            raise ValueError(f"Battle source doesn't match extraction report: {name}")
        counts[name] = len(rows)
        index = {}
        for number, raw in rows:
            row = dict(raw)
            dynamic = {key: parse(row.pop(key), name, number, key)
                       for key in list(row) if "DynamicField" in key}
            if dynamic:
                row["Dynamic"] = dynamic
            identifier = row.get(LEVEL_KEYS.get(name, "ID"))
            if not identifier:
                raise ValueError(f"Invalid battle ID: {name}:{number}")
            if name == "Scene" and not identifier.startswith(("MysteriousRealm_", "BH")):
                continue
            if name == "Scene" and identifier.startswith("BH"):
                row["Waves"] = [parse(row.pop(key), name, number, key)
                                for key in ("WaveInfoJsonString", "WaveInfo2JsonString", "WaveInfo3JsonString") if row.get(key)]
                for key in ("MyCampTeam", "RoleStatusInfo1", "RoleStatusInfo2"):
                    if row.get(key):
                        row[key] = parse(row[key], name, number, key)
            if name == "AIAction":
                row = {"Priority": int(row.get("Priority") or 0), "SkillIndex": int(row.get("SkillIndex") or 0),
                       "SourceLine": number,
                       "Conditions": [parse(row[key], name, number, key) for key in ("Condition1", "Condition2", "Condition3") if key in row],
                       "Targets": [parse(row[key], name, number, key) for key in (f"TargetSetting{i}" for i in range(1, 6)) if key in row]}
                for target in row["Targets"]:
                    if isinstance(target.get("Type"), str):
                        census["TargetType"][target["Type"]] += 1
                index.setdefault(identifier, []).append(row)
            elif name == "Fantasy":
                row = {"ActivityIDs": row.get("ActivityIDs", "").split("~") if row.get("ActivityIDs") else [],
                       "Waves": [{"Monsters": parse(row[f"WaveMonsterJson{i}"], name, number, f"WaveMonsterJson{i}").get("Ms", []),
                                  "Buffs": parse(row[f"WaveBuffJson{i}"], name, number, f"WaveBuffJson{i}")}
                                 for i in (1, 2)]}
                index.setdefault(identifier, []).append(row)
            elif name in LEVEL_KEYS:
                level = str(row.pop("LV"))
                row.pop(LEVEL_KEYS[name])
                levels = index.setdefault(identifier, {})
                if level in levels:
                    raise ValueError(f"Duplicate battle level: {name}:{identifier}:{level}")
                levels[level] = row
            else:
                if identifier in index:
                    raise ValueError(f"Duplicate battle ID: {name}:{identifier}")
                row.pop("ID")
                index[identifier] = row
        tables[name] = index
    for rules in tables["AIAction"].values():
        rules.sort(key=lambda rule: -rule["Priority"])  # Stable for equal priority.
    missing = []
    for sid, configs in tables["Fantasy"].items():
        for config in configs:
            for wave in config["Waves"]:
                for monster in wave["Monsters"]:
                    if monster["M"] not in tables["Role"]:
                        missing.append(f"{sid}:Role:{monster['M']}")
                for buffs in wave["Buffs"].values():
                    for buff in buffs:
                        if buff["B"] not in tables["Status"]:
                            missing.append(f"{sid}:Status:{buff['B']}")
    if missing:
        raise ValueError(f"Missing battle references: {sorted(set(missing))}")
    evidence = {"rows": counts, "table_sha256": hashes, "unparsed_fields": unresolved,
                "selectors": {key: dict(sorted(counts.items())) for key, counts in census.items()}}
    return {"schema": 1, "version": provenance["version"], "table_sha256": hashes, "tables": tables}, evidence
