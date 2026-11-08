"""从游戏静态数据表生成 equipment/stat_tables.json（角色面板计算 / 自动配装用）。

数据来源：WebGL 资源包 firstbundlegroup_assets_assets/game/staticdata_*.bundle
（PatchDomain 由 GameServerDBSettingHandler.QueryBulletinInfoResult 的 PathDomains 下发），
用 UnityPy 解出 TextAsset 后得到 Role.txt、RoleProperty.txt 等 '@' 分隔的表。

用法: python -m equipment.gen_stat_tables <解出的 txt 目录>
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "stat_tables.json"

STATS = ("HP", "Attack", "Defence", "Speed", "CriticalRate", "CriticalDamageRate",
         "EffectHitRate", "ResistanceRate", "PinchRate")
MODIFIERS = {"HPValue", "AttackValue", "DefenceValue", "SpeedValue", "HPRate", "AttackRate",
             "DefenceRate", "SpeedRate", *STATS[4:]}


def rows(path: Path) -> list[dict]:
    lines = path.read_bytes().decode("utf-8-sig", "replace").splitlines()
    head = lines[0].split("@")
    return [dict(zip(head, line.split("@"))) for line in lines[1:] if line.strip()]


def num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def bonus(row: dict, keys) -> dict:
    return {k: num(row.get(k)) for k in keys if num(row.get(k))}


def direct_skill_mods(row: dict, passive: bool) -> dict:
    """主动技能仅取 PassiveProp；被动技能还取其直接 DynamicField 属性。"""
    mods = {}
    for key, text in row.items():
        if not (key.startswith("PassiveProp.") or passive and key.startswith("DynamicField")):
            continue
        prop, _, value = (text or "").partition("#")
        if prop not in MODIFIERS:
            continue
        try:
            amount = float(value)
        except ValueError:
            continue
        if math.isfinite(amount) and amount:
            mods[prop] = mods.get(prop, 0.0) + amount
    return mods


def role_skill_ids(role: dict) -> set[str]:
    # :Hide 是技能的显示标记，不是 StaticID 的一部分。
    return {sid.partition(":")[0] for sid in (role.get("SkillIDs") or "").split("~") if sid}


def skill_passives(role_rows: list[dict], skill_rows: list[dict]) -> dict:
    """技能中直接声明的永久面板属性；不把 Fun 战斗触发效果当成常驻加成。"""
    by_skill = {row["ID"]:direct_skill_mods(row,row.get("SkillType")=="Passive") for row in skill_rows}
    by_role = {}
    for role in role_rows:
        if role.get("IsHero") != "1":
            continue
        mods = {}
        for sid in role_skill_ids(role):
            for prop, amount in by_skill.get(sid, {}).items():
                mods[prop] = mods.get(prop, 0.0) + amount
        if mods:
            by_role[role["ID"]] = mods
    return by_role


def skill_level_passives(role_rows: list[dict], skill_rows: list[dict], level_rows: list[dict]) -> dict:
    skill_types = {row["ID"]:row.get("SkillType") for row in skill_rows}
    by_skill = {}
    for row in level_rows:
        sid = row.get("SkillID")
        if sid not in skill_types:
            continue
        mods = direct_skill_mods(row,skill_types[sid]=="Passive")
        if mods:
            by_skill.setdefault(sid,[]).append({"lv":int(num(row.get("LV"))),**mods})
    return {role["ID"]:{sid:sorted(by_skill[sid],key=lambda r:r["lv"])
                        for sid in role_skill_ids(role) if sid in by_skill}
            for role in role_rows if role.get("IsHero")=="1" and role_skill_ids(role).intersection(by_skill)}


def imprint_bonuses(imprint_rows: list[dict]) -> dict:
    out = {}
    for row in imprint_rows:
        prop, _, increment = (row.get("LevelAdd.DynamicField1") or "").partition("#")
        if not prop:
            continue
        base_prop, _, initial = (row.get("Base.DynamicField1") or "").partition("#")
        if base_prop and base_prop != prop:
            raise ValueError(f"潜能基础属性与等级增量不一致：{row['ID']}")
        out[row["ID"]] = {"prop": prop, "base": num(initial), "add": num(increment)}
    return out


def build(src: Path) -> dict:
    sets = {}
    for r in rows(src / "EquipmentSet.txt"):
        rates = {k: num(r[k]) for k in r if k.endswith("Rate") and num(r[k])}
        sets[r["ID"]] = {"count": int(num(r["Count"])), "rates": rates}

    roles = {}
    role_rows = rows(src / "Role.txt")
    for r in role_rows:
        if r.get("IsHero") != "1":
            continue
        roles[r["ID"]] = {
            "prop": r.get("RolePropertyID") or "HERO",
            "mult": {k: num(r.get(k)) for k in STATS},
            "selfImprint": r.get("SelfImprint") or "",
        }

    used = {v["prop"] for v in roles.values()}
    props: dict[str, dict] = {}
    for r in rows(src / "RoleProperty.txt"):
        if r["ID"] in used:
            props.setdefault(r["ID"], {})[r["LV"]] = {k: num(r.get(k)) for k in STATS}

    awaken: dict[str, list] = {}
    awaken_keys = ("HPValue", "AttackValue", "HPRate", "AttackRate", "DefenceRate", "SpeedValue",
                   "CriticalRate", "CriticalDamageRate", "PinchRate", "EffectHitRate", "ResistanceRate")
    for r in rows(src / "RoleAwaken.txt"):
        awaken.setdefault(r["RoleID"], []).append({"lv": int(num(r["LV"])), **bonus(r, awaken_keys)})

    imprint = imprint_bonuses(rows(src / "RoleImprint.txt"))

    artifacts = {}
    for r in rows(src / "Artifact.txt"):
        artifacts[r["ID"]] = {
            "atk": [num(r.get("Base.AttackValue")), num(r.get("Max.AttackValue"))],
            "hp": [num(r.get("Base.HPValue")), num(r.get("Max.HPValue"))],
        }

    # 装备 StaticID -> 部位（游戏 Equipment 表的 Part 字段，部位识别的第一依据）
    part_slot = {"Weapon": "weapon", "Head": "helmet", "Body": "armor",
                 "Necklace": "necklace", "Ring": "ring", "Shoes": "boots"}
    parts = {r["ID"]: part_slot[r["Part"]] for r in rows(src / "Equipment.txt") if r.get("Part") in part_slot}

    skill_rows = rows(src / "Skill.txt")
    out = {"sets": sets, "roles": roles, "props": props, "awaken": awaken,
           "imprint": imprint, "artifacts": artifacts, "artifactMaxLV": 30, "parts": parts,
           "skillPassives": skill_passives(role_rows, skill_rows),
           "skillPassiveLevels": skill_level_passives(role_rows,skill_rows,rows(src / "SkillLevel.txt"))}
    return out


def main(src: Path) -> None:
    out = build(src)
    TARGET.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {TARGET} ({TARGET.stat().st_size} bytes): {len(out['sets'])} sets, {len(out['roles'])} roles, {len(out['parts'])} equipment parts")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
