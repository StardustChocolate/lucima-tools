"""Shared client selectors, conditions and float32 value conversions.

The context contains only current battle state. Event-only fields are rejected
when no event exists, rather than invented during a main-round AI decision.
Native references: SkillCondition 0x2235658, SkillSelectInfo 0x2249828,
SkillValueConvert 0x22500a4 and GetPositionRowRole 0x21e21c0.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import random

from .abyss_state import enabled, float32, metadata, status_properties


class EvaluationError(ValueError):
    pass


def alive(unit):
    return not (unit.get("NowIsDieOut") or unit.get("IsDieOut") or unit.get("IsDead")) and float(unit.get("NowHP", 1) or 0) > 0


def hp_rate(unit):
    maximum = float(unit.get("MaxHP", unit.get("_ObservedHPUpperBound", 0)) or 0)
    hp = float(unit.get("NowHP", 0) or 0)
    return float32(float32(hp) / float32(maximum)) if maximum > 0 else float(hp > 0)


def camp(unit):
    return str(unit.get("Camp") or str(unit.get("id", "")).split("-", 1)[0])


def same(a, b):
    return a is b or (a is not None and b is not None and a.get("id") is not None and a.get("id") == b.get("id"))


def position(unit):
    try:
        return int(unit.get("Position", str(unit.get("id", "0-0-99")).rsplit("-", 1)[-1]))
    except (TypeError, ValueError):
        return 99


@dataclass
class Context:
    source: dict
    allies: list[dict]
    enemies: list[dict]
    tables: dict | None = None
    target: dict | None = None
    aura_source: dict | None = None
    round_role: str | None = None
    skill: dict | None = None
    rng: random.Random = field(default_factory=random.Random)
    issues: set[str] = field(default_factory=set)

    def __post_init__(self):
        if self.tables is None:
            self.tables = metadata()["tables"]

    def for_target(self, unit):
        return replace(self, target=unit)

    def members(self, unit):
        return [other for other in self.allies + self.enemies if camp(other) == camp(unit)]

    def flag(self, unit, name):
        return enabled(unit.get(name)) or enabled(unit.get("Is" + name)) or float(unit.get("_AbilityProperties", {}).get(name, 0) or 0) > 0 or float(status_properties(unit, self.tables).get(name, 0) or 0) > 0


def statuses(unit, ctx, *, kind=None, identifier=None, group=False):
    result = []
    for status in unit.get("NowStatusList") or []:
        row = ctx.tables["Status"].get(str(status.get("StaticID")), {})
        category = status.get("IsBuffType", row.get("IsBuffType", "Debuff"))
        if kind is not None and not (category in (("", "Debuff", "DeBuff", 0) if kind == "Debuff" else ("Buff", 1))):
            continue
        key = row.get("Group", "") if group else status.get("StaticID")
        if identifier and str(key) not in str(identifier).split("~"):
            continue
        result.append(status)
    return result


def conquer(a, b):
    codes = {"Dark": 0, "Fire": 1, "Earth": 2, "Ice": 3, "Light": 4}
    return (codes.get(a.get("Element"), a.get("Element")), codes.get(b.get("Element"), b.get("Element"))) in {(1, 3), (3, 2), (2, 1), (4, 0), (0, 4)}


def property_value(name, unit, ctx, identifier=""):
    name = {"DeBuffCount": "DebuffCount", "CampDeBuffCount": "CampDebuffCount"}.get(name, name)
    if unit is None:
        raise EvaluationError("property_without_subject:" + name)
    if name == "ValueOne": return 1.0
    if name == "NowHPRate": return hp_rate(unit)
    if name == "LostHPRate": return float32(1 - hp_rate(unit))
    if name == "LastHPRate":
        # Despite its name this native branch (0x224ebf8) is lost HP * 100.
        maximum = float(unit.get("MaxHP", 0) or 0)
        return float32(float32(float32(maximum - float(unit.get("NowHP", 0))) / float32(maximum)) * 100) if maximum else 0.0
    if name in {"HP", "MaxHP"}: return float(unit.get("MaxHP", 0))
    if name == "NoInjuryMaxHP": return float(unit.get("NoInjuryMaxHP", unit.get("MaxHP", 0)))
    if name in {"BuffCount", "DebuffCount", "CanRemoveBuffCount", "CanRemoveDebuffCount"}:
        return float(len(statuses(unit, ctx, kind="Debuff" if "Debuff" in name else "Buff")))
    if name in {"BuffStack", "BuffGroupStack", "BuffValue", "BuffCountByID", "BuffCountByGroup", "BuffAllRoundByID", "BuffMinRoundByID"}:
        selected = statuses(unit, ctx, identifier=identifier, group=name in {"BuffGroupStack", "BuffCountByGroup"})
        if name in {"BuffCountByID", "BuffCountByGroup"}: return float(len(selected))
        key = "Value" if name == "BuffValue" else "NowStack" if name in {"BuffStack", "BuffGroupStack"} else "NowRound"
        values = [float(s.get(key, 0) or 0) for s in selected]
        return min(values, default=0) if name == "BuffMinRoundByID" else sum(values)
    members = ctx.members(unit)
    if name in {"CampBuffCount", "CampDebuffCount"}:
        return float(sum(len(statuses(other, ctx, kind="Debuff" if name == "CampDebuffCount" else "Buff")) for other in members))
    if name == "CampAverageHPRate":
        living = [other for other in members if alive(other)]
        return float32(sum(hp_rate(other) for other in living) / len(living)) if living else 0.0
    if name in {"AllyCount", "EnemyCount", "FewEnemyCount", "AllyDeadCount", "DeadCount"}:
        others = members if name.startswith("Ally") else [other for other in ctx.allies + ctx.enemies if camp(other) != camp(unit)]
        return float(sum(not alive(other) if "Dead" in name else alive(other) for other in others))
    if name in {"CampJobCount", "CampElementCount"}:
        key = "Job" if name == "CampJobCount" else "Element"
        return float(sum(alive(other) and str(other.get(key)) in str(identifier).split("~") for other in members))
    if name in {"NowTime", "Time"}: return float((unit.get("RoleTimeInfo") or {}).get("NowTimeLength", unit.get("NowTime", 0)))
    if name == "RoleLV": return float(unit.get("LV", 1))
    if name == "CoolDown": return float(max((unit.get("NowSkillCooldownMap") or {}).values(), default=0))
    if name == "LastHP":
        raise EvaluationError("event_property_without_event:" + name)
    if name in unit and isinstance(unit[name], (float, int)):
        return float32(unit[name])
    props = unit.get("_AbilityProperties", {})
    if name in props and isinstance(props[name], (float, int)):
        return float32(props[name])
    raise EvaluationError("unknown_property:" + name)


def subjects(who, selector, ctx):
    if selector:
        return select(selector, ctx)
    if who in {None, "", "Target"}: return [ctx.target or ctx.source]
    if who in {"Source", "Self", "TargetSetting"}: return [ctx.source]
    if who == "AuraSource": return [ctx.aura_source] if ctx.aura_source is not None else []
    if who == "SelectTarget": return [ctx.target] if ctx.target is not None else []
    raise EvaluationError("context_unavailable:" + str(who))


def compare(value, cond, ctx):
    threshold = value_convert(cond["ComparedValue"], ctx) if cond.get("ComparedValue") else float32(float(cond.get("Value", 0) or 0))
    second = float32(float(cond.get("Value2", 0) or 0))
    comparer = cond.get("Comparer") or "Eq"
    value = float32(value)
    if comparer == "Eq": return value == threshold
    if comparer == "Ne": return value != threshold
    if comparer == "Gt": return value >= threshold
    if comparer == "Gtn": return value > threshold
    if comparer == "Lt": return value <= threshold
    if comparer == "Ltn": return value < threshold
    if comparer == "Between": return min(threshold, second) <= value <= max(threshold, second)
    raise EvaluationError("unknown_comparer:" + str(comparer))


def check_subject(cond, unit, ctx):
    fn = cond.get("CheckFun") or "None"
    ids = cond.get("CheckIDs") or str(cond.get("CheckID") or "").split("~")
    if isinstance(ids, str): ids = ids.split("~")
    if fn in {"HaveBuff", "NoBuff", "HaveBuffGroup", "HasAnyBuff"}:
        result = any(statuses(unit, ctx, identifier=i, group=fn == "HaveBuffGroup") for i in ids if i)
        if fn == "NoBuff": result = not result
    elif fn in {"AnyEnemyHaveBuff", "AnyAllyHaveBuff", "EnemyHaveDeBuff", "AllyHaveDeBuff", "AnyEnemyHaveCoolDown", "AnyAllyHaveCoolDown"}:
        own = "Ally" in fn
        others = [other for other in ctx.allies + ctx.enemies if (camp(other) == camp(unit)) == own and alive(other)]
        if "CoolDown" in fn: result = any(any(float(v or 0) > 0 for v in (other.get("NowSkillCooldownMap") or {}).values()) for other in others)
        elif "DeBuff" in fn: result = any(statuses(other, ctx, kind="Debuff") for other in others)
        else: result = any(statuses(other, ctx, identifier=i) for other in others for i in ids if i)
    elif fn in {"CheckSkillCooldown", "CheckSkillNoUse"}:
        cooling = any(float((unit.get("NowSkillCooldownMap") or {}).get(i, 0) or 0) > 0 for i in ids)
        # Native 0x22365e0 is CD < 1; 0x2236abc is CD > 0.
        result = not cooling if fn == "CheckSkillCooldown" else cooling
    elif fn in {"RoleJob", "RoleElement"}: result = str(unit.get("Job" if fn == "RoleJob" else "Element")) in ids
    elif fn == "Position": result = str(position(unit)) in ids
    elif fn == "IsBoss": result = enabled(unit.get("IsBoss"))
    elif fn == "IsHero": result = enabled(ctx.tables["Role"].get(unit.get("StaticID"), {}).get("IsHero"))
    elif fn in {"IsSelf", "IsNoSelf"}: result = same(unit, ctx.source) == (fn == "IsSelf")
    elif fn in {"IsSameCamp", "IsDiffCamp"}: result = (camp(unit) == camp(ctx.source)) == (fn == "IsSameCamp")
    elif fn == "IsSameRole": result = unit.get("StaticID") == ctx.source.get("StaticID")
    elif fn in {"IsMainRound", "IsNotMainRound"}:
        if ctx.round_role is None: raise EvaluationError("round_role_unavailable")
        result = (unit.get("id") == ctx.round_role) == (fn == "IsMainRound")
    elif fn == "NotMaxHP": result = hp_rate(unit) < 1
    elif fn == "OtherAllyHurt": result = any(not same(other, unit) and alive(other) and hp_rate(other) < 1 for other in ctx.members(unit))
    elif fn in {"HPHeighSource", "HPLessSource"}: result = hp_rate(unit) > hp_rate(ctx.source) if fn == "HPHeighSource" else hp_rate(unit) < hp_rate(ctx.source)
    elif fn == "SkillID": result = ctx.skill is not None and ctx.skill.get("StaticID") in ids
    elif fn == "IsNormalSkill": result = ctx.skill is not None and ctx.skill.get("StaticID") == ((ctx.source.get("Skills") or {}).get("Skills") or [{}])[0].get("StaticID")
    elif fn in {"None", ""}: result = True
    else: raise EvaluationError("unknown_condition:" + fn)
    if cond.get("Property"):
        result = result and compare(property_value(cond["Property"], unit, ctx, str(cond.get("CheckID") or "")), cond, ctx)
    return result


def check(cond, ctx):
    if not isinstance(cond, dict): return True
    # NextCondition / Conditions are mandatory gates outside this node's Invert.
    if cond.get("NextCondition") and not check(cond["NextCondition"], ctx): return False
    if not all(check(c, ctx) for c in cond.get("Conditions") or []): return False
    units = subjects(cond.get("Who"), cond.get("TargetInfo") or cond.get("RoleSelectInfo"), ctx)
    if not units: return False
    result = all(check_subject(cond, unit, ctx) for unit in units) if enabled(cond.get("IsAllWhoPass")) else any(check_subject(cond, unit, ctx) for unit in units)
    return not result if enabled(cond.get("Invert")) else result


def value_convert(converter, ctx):
    selected = subjects(converter.get("Who") or "Source", converter.get("TargetInfo"), ctx)
    unit = selected[0] if selected else None
    target_ctx = ctx.for_target(unit)
    valid = (not converter.get("Condition") or check(converter["Condition"], target_ctx)) and all(check(c, target_ctx) for c in converter.get("Conditions") or [])
    raw = property_value(converter.get("Property") or "None", unit, ctx, converter.get("PropertyID", "")) if valid and converter.get("Property") not in {None, "", "None"} else 0.0
    maximum = value_convert(converter["MaxValueConvert"], ctx) if converter.get("MaxValueConvert") else float32(float(converter.get("MaxValue", 9999999)))
    raw = min(raw, maximum) if raw > 0 else max(raw, -abs(maximum)) if raw < 0 else raw
    rate = float32(float(converter.get("Rate", 0) or 0))
    for choice in converter.get("ConditionRates") or []:
        if not choice.get("Condition") or check(choice["Condition"], target_ctx):
            rate = float32(float(choice.get("Rate", 0) or 0))
            break
    extra = value_convert(converter["ExtraRate"], ctx) if converter.get("ExtraRate") else 1.0
    extra = float32(extra + float32(float(converter.get("ExtraRatePlus", 0) or 0)))
    addition = value_convert(converter["ValueConvert"], ctx) if converter.get("ValueConvert") else 0.0
    value = float32(float32(float32(raw * rate) * extra) + float32(float(converter.get("Base", 0) or 0)))
    value = float32(value + addition)
    if converter.get("AddRandom"): value = float32(value + ctx.rng.uniform(0, float(converter["AddRandom"])))
    final_max = value_convert(converter["FinalMaxValueConvert"], ctx) if converter.get("FinalMaxValueConvert") else float32(float(converter.get("FinalMaxValue", 9999999)))
    if value > 0: return max(min(value, final_max), float32(float(converter.get("FinalMinValue", 0))))
    if value < 0: return 0.0 if enabled(converter.get("MustPositive")) else max(value, -abs(final_max))
    return value


def select(info, ctx):
    typ = info.get("TargetType") or info.get("Type") or "One"
    own = str(info.get("TargetCamp") or "Enemy") in {"My", "Self", "Ally", "1"}
    pool = ctx.allies if own else ctx.enemies
    include_dead = typ in {"DeadOne", "OneWithDead", "AllWithDead"}
    eligible = [u for u in pool if (alive(u) or include_dead and not u.get("NowIsDieOut"))
                and (enabled(info.get("IncludeHideOut")) or not ctx.flag(u, "HideOut"))
                and (own or enabled(info.get("IncludeStealth", True)) or not ctx.flag(u, "Stealth"))
                and (own or enabled(info.get("IncludeInvincible", True)) or not ctx.flag(u, "Invincible"))
                and not (enabled(info.get("NoSelf")) and same(u, ctx.source))
                and not (enabled(info.get("RemoveWillPassRound")) and any(ctx.flag(u, flag) for flag in ("Dizzy", "Sleep", "HideOut")))]
    if info.get("Condition"): eligible = [u for u in eligible if check(info["Condition"], ctx.for_target(u))]
    living = [u for u in eligible if alive(u)]
    identifier = str(info.get("TargetTypeID") or info.get("PropertyID") or info.get("CheckID") or "")
    if typ == "Self": return [ctx.source] if alive(ctx.source) and not enabled(info.get("NoSelf")) and not ctx.flag(ctx.source, "HideOut") else []
    if typ in {"AuraSource", "ParamTarget", "SkillSelect"}:
        u = ctx.aura_source if typ == "AuraSource" else ctx.target
        return [u] if u is not None else []
    if typ == "DeadOne": return [u for u in eligible if not alive(u)]
    if typ in {"AllWithDead", "OneWithDead"}: return eligible
    if typ in {"All", "One", "SkillSetting"}: return living
    if typ == "Boss": return [u for u in living if enabled(u.get("IsBoss"))]
    if typ in {"FrontRow", "BackRow"}: return [u for u in living if position(u) == (0 if typ == "FrontRow" else 3)][:1]
    if typ == "ConquerTarget": return [u for u in living if conquer(ctx.source, u)]
    if typ == "ConquerOKTarget": return [u for u in living if conquer(ctx.source, u)] or [u for u in living if not conquer(u, ctx.source)]
    if typ in {"HaveDeBuffOne", "HaveDeBuffAll"}: return [u for u in living if statuses(u, ctx, kind="Debuff")]
    if typ == "HaveCoolDown": return [u for u in living if any(float(v or 0) > 0 for v in (u.get("NowSkillCooldownMap") or {}).values())]
    if typ in {"Job", "Element"}: return [u for u in living if str(u.get(typ)) in identifier.split("~")]
    if typ == "TautSource":
        ids = {s.get("SourceRoleID") for s in ctx.source.get("NowStatusList") or [] if status_properties({"NowStatusList": [s]}, ctx.tables).get("Taunt")}
        return [u for u in living if u.get("id") in ids]
    if typ in {"RandomOne", "RandomTwo", "RandomOneTwo"}:
        if typ == "RandomOne": return [ctx.rng.choice(living)] if living else []
        count = 1 if typ == "RandomOne" else 2 if typ == "RandomTwo" else ctx.rng.randint(1, 2)
        # RandomTwo removes random members until two remain; final order is position.
        chosen = list(living)
        while len(chosen) > count: chosen.pop(ctx.rng.randrange(len(chosen)))
        return sorted(chosen, key=position)
    keys = {"LessHP": "NowHP", "TwoLessHP": "NowHP", "HighestHPOne": "NowHP",
            "LessHPRate": "NowHPRate", "TwoLessHPRate": "NowHPRate", "HighestHPRate": "NowHPRate",
            "HightestAtkOne": "Attack", "HightestSkillCD": "CoolDown", "MostTime": "NowTime",
            "MaxBuffStack": "BuffStack", "MaxBuffGroupStack": "BuffGroupStack",
            "HighestMaxHP": "MaxHP", "LowMaxHP": "MaxHP"}
    prop = info.get("Property") if typ in {"MaxProperty", "MinProperty"} else keys.get(typ)
    if not prop: raise EvaluationError("unknown_selector:" + str(typ))
    descending = typ not in {"LessHP", "TwoLessHP", "LessHPRate", "TwoLessHPRate", "LowMaxHP", "MinProperty"}
    ordered = sorted(living, key=lambda u: position(u))
    ordered.sort(key=lambda u: property_value(prop, u, ctx, identifier), reverse=descending)
    return ordered[:2 if typ in {"TwoLessHP", "TwoLessHPRate"} else 1]
