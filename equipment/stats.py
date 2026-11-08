"""角色面板计算：基础 × (1 + Σ百分比) + Σ固定值。

数据来自 equipment/stat_tables.json（由 equipment/gen_stat_tables.py 从游戏静态表生成）：
- 基础值 = RoleProperty[RolePropertyID][LV] × Role 表里该角色的倍率
  （璐茜玛 Lv60：防御 617×1.107=683、速度 100×1.06=106，与游戏面板一致）
- 面板显示：攻防生速四舍五入；神器数值先取整
- 觉醒：RoleAwaken 中 LV ≤ AwakenLV 的各行累加
- 技能：直接声明的常驻属性及其 SkillLevel 增量；按 RoleData.Skills.Skills 的实际 Level 累加
  主动技能只取 PassiveProp，被动技能也取直接 DynamicField 属性；战斗触发效果不计
- 潜能突破：只算个人突破（IsSelfImprint），已开发时值 = Base + LevelAdd × (ImprintLV - 1)
  ImprintLV 1 为 D 级；团队突破是战斗内光环，不进面板
- 羁绊：默认保留原始面板口径；装备页和筛选显式启用 include_artifact_effects，
  计入基础攻击/生命、自身常驻属性及最终倍率，排除战斗触发、条件与队伍光环
- 装备：主属性 + 副属性；套装按件数折算完成的套数（2 件套可叠两次）
暴击率等比率属性直接相加，单位为小数（0.85 = 85%）。
"""
from __future__ import annotations

import json
import os

STATS = ("HP", "Attack", "Defence", "Speed", "CriticalRate", "CriticalDamageRate",
         "EffectHitRate", "ResistanceRate", "PinchRate")
# 会被百分比放大的四项：XxxRate 乘基础值，XxxValue 直接加
_SCALED = {"HP": ("HPRate", "HPValue"), "Attack": ("AttackRate", "AttackValue"),
           "Defence": ("DefenceRate", "DefenceValue"), "Speed": ("SpeedRate", "SpeedValue")}
STAT_CN = {"HP": "生命", "Attack": "攻击", "Defence": "防御", "Speed": "速度",
           "CriticalRate": "暴击率", "CriticalDamageRate": "暴击伤害",
           "EffectHitRate": "状态命中", "ResistanceRate": "状态抗性", "PinchRate": "追击率"}
PERCENT_STATS = {"CriticalRate", "CriticalDamageRate", "EffectHitRate", "ResistanceRate", "PinchRate"}

_TABLES = None


def tables() -> dict:
    global _TABLES
    if _TABLES is None:
        from backend.resource_store import path as resource_path
        path = resource_path("equipment/stat_tables.json")
        with open(path, encoding="utf-8") as f:
            _TABLES = json.load(f)
    return _TABLES


def _oid(v):
    return v.get("$oid") if isinstance(v, dict) else v


def add(mods: dict, key: str, value) -> None:
    if value:
        mods[key] = mods.get(key, 0.0) + float(value)


def role_base(role: dict) -> dict:
    t = tables()
    info = t["roles"].get(str(role.get("StaticID") or ""))
    if not info:
        return {}
    levels = t["props"].get(info["prop"], {})
    row = levels.get(str(role.get("LV") or 1)) or {}
    return {k: row.get(k, 0.0) * info["mult"].get(k, 0.0) for k in STATS}


def role_mods(state: dict, role: dict, *, include_artifact_effects=False) -> dict:
    """觉醒、个人潜能突破、神器带来的加成（不含装备）。"""
    t = tables()
    sid = str(role.get("StaticID") or "")
    mods: dict = {}
    for k, v in (t.get("skillPassives") or {}).get(sid, {}).items():
        add(mods, k, v)
    # 实际协议结构：Skills: {Skills: [{StaticID: "H192S2", Level: 8}, ...]}。
    # 未强化或没有记录时只算基础技能属性，不能假定满级以免放过未满暴击的配装。
    skill_data = role.get("Skills") or {}
    skill_levels = {r["StaticID"]:int(r.get("Level") or 1) for r in skill_data.get("Skills") or []}
    for skill_id, rows in (t.get("skillPassiveLevels") or {}).get(sid, {}).items():
        for row in rows:
            if row["lv"] <= skill_levels.get(skill_id,1):
                for k, v in row.items():
                    if k != "lv":
                        add(mods, k, v)
    awaken_lv = int(role.get("AwakenLV") or 0)
    for row in t["awaken"].get(sid, []):
        if row["lv"] <= awaken_lv:
            for k, v in row.items():
                if k != "lv":
                    add(mods, k, v)
    imprint_lv = int(role.get("ImprintLV") or 0)
    info = t["roles"].get(sid) or {}
    imp = t["imprint"].get(info.get("selfImprint") or "")
    if imp and imprint_lv > 0 and role.get("IsSelfImprint"):
        # 第 1 档取 Base，之后才累加 LevelAdd。五星暴击 B 档（LV3）为 5.6%，非 8.4%。
        add(mods, imp["prop"], imp.get("base", 0.0) + imp["add"] * (imprint_lv - 1))
    rid = _oid(role.get("_id"))
    artifacts = [art for art in (state.get("Artifacts") or {}).get("Datas") or [] if _oid(art.get("EquipRole")) == rid]
    if include_artifact_effects and len(artifacts) > 1:
        from backend.game_client import GameError
        raise GameError("角色羁绊归属异常，请同步账号后再筛选")
    for art in artifacts:
        if _oid(art.get("EquipRole")) != rid:
            continue
        ref = t["artifacts"].get(str(art.get("StaticID") or ""))
        if ref and not include_artifact_effects:
            frac = max(0, min(1, (int(art.get("LV") or 1) - 1) / (t["artifactMaxLV"] - 1)))
            # 游戏先截去神器插值的小数，再加到角色面板；并非四舍五入。
            # 例：Lv15 的 142.655... 攻击计 142，否则 H121 两套截图均多 1 攻击。
            add(mods, "AttackValue", int(ref["atk"][0] + (ref["atk"][1] - ref["atk"][0]) * frac))
            add(mods, "HPValue", int(ref["hp"][0] + (ref["hp"][1] - ref["hp"][0]) * frac))
        if include_artifact_effects:
            from .bonds import permanent_mods
            from backend.game_client import GameError
            # Full-effect panels use the versioned battle definition below.
            # The legacy interpolation index is only needed by the old panel
            # mode; its absence must not reject a bond already fully described.
            extra, _ = permanent_mods(art)
            if not extra:
                raise GameError("缺少当前羁绊属性资料，请更换羁绊后再筛选")
            for key, value in extra.items():
                add(mods, key, value)
    return mods


def equip_mods(equip: dict) -> dict:
    mods: dict = {}
    main = equip.get("MainProp") or {}
    add(mods, main.get("PropertyType") or "", main.get("Value"))
    for sub in (equip.get("SubProps") or {}).get("SourceValues") or []:
        add(mods, sub.get("PropertyType") or "", sub.get("Value"))
    return mods


def set_mods(set_counts: dict) -> tuple[dict, list[str]]:
    """{套装: 件数} -> (套装加成, 已生效的套装列表)。"""
    sets = tables()["sets"]
    mods: dict = {}
    active = []
    for name, pieces in set_counts.items():
        info = sets.get(name)
        if not info:
            continue
        times = pieces // info["count"]
        for _ in range(times):
            active.append(name)
            for k, v in info["rates"].items():
                add(mods, k, v)
    return mods, active


def panel(base: dict, mods: dict) -> dict:
    out = {}
    for stat in STATS:
        if stat in _SCALED:
            rate, flat = _SCALED[stat]
            out[stat] = (base.get(stat, 0.0) * (1 + mods.get(rate, 0.0)) + mods.get(flat, 0.0)) * (1 + mods.get(stat + "FinalRate", 0.0))
        else:
            out[stat] = base.get(stat, 0.0) + mods.get(stat, 0.0)
    return out


def contribution(base: dict, mods: dict, final_mods: dict | None = None) -> dict:
    """一组加成在最终面板上的增量（线性，可逐件相加）。"""
    out = {}
    for stat in STATS:
        if stat in _SCALED:
            rate, flat = _SCALED[stat]
            out[stat] = (base.get(stat, 0.0) * mods.get(rate, 0.0) + mods.get(flat, 0.0)) * (1 + (final_mods or {}).get(stat + "FinalRate", 0.0))
        else:
            out[stat] = mods.get(stat, 0.0)
    return out


def display(values: dict) -> dict:
    """面板取整：比率显示为百分比（1 位小数），其余四舍五入（与游戏面板一致：
    安泰西亚生命 9521.75 显示 9522）。"""
    out = {}
    for stat, v in values.items():
        out[stat] = round(v * 100, 1) if stat in PERCENT_STATS else int(v + 0.5 + 1e-9)
    return out


def role_panel(state: dict, role: dict, equips: list[dict], *, include_artifact_effects=False) -> dict:
    base = role_base(role)
    mods = dict(role_mods(state, role, include_artifact_effects=include_artifact_effects))
    counts: dict = {}
    for eq in equips:
        for k, v in equip_mods(eq).items():
            add(mods, k, v)
        if eq.get("Set"):
            counts[eq["Set"]] = counts.get(eq["Set"], 0) + 1
    smods, active = set_mods(counts)
    for k, v in smods.items():
        add(mods, k, v)
    return {"panel": display(panel(base, mods)), "sets": active}
