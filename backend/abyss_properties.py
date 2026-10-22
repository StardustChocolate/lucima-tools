"""Client ability synthesis: base, formation, statuses, passives and auras.

Numeric accumulation uses doubles; converters and final getters use float32.
Native references and constants are recorded in docs/abyss-implementation.md.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field

from .abyss_state import direct_properties, enabled, float32, status_properties
from .abyss_rules import Context, EvaluationError, alive, camp, check, position, same, value_convert
from .game_client import GameError


BASE_STATS = ("HP", "Attack", "Defence", "Speed", "CriticalRate", "CriticalDamageRate",
              "EffectHitRate", "ResistanceRate", "PinchRate")
AWAKEN_STATS = ("HPValue", "AttackValue", "HPRate", "AttackRate", "DefenceRate", "SpeedValue",
                "CriticalRate", "CriticalDamageRate", "PinchRate", "EffectHitRate", "ResistanceRate")
SET_STATS = ("HPRate", "AttackRate", "DefenceRate", "SpeedRate", "CriticalRate", "CriticalDamageRate",
             "PinchRate", "CounterRate", "EffectHitRate", "ResistanceRate", "LifeStealRate")

# Defaults from the recovered native bodies, not table estimates.
MAX_STAR = 6                    # RoleStaticData..cctor 0x21fb484
MAX_ARTIFACT_LEVEL = 30          # ArtifactData..cctor 0x21178b8, ELF 0xd89824
ROLE_STAR_RATE = 0.0560000017285347  # PublicVariableStaticData.RoleStarRate, ELF 0xd89d38


@dataclass
class LiteralProperties:
    values: dict[str, float] = field(default_factory=dict)
    deferred: list[str] = field(default_factory=list)
    functions: list[dict] = field(default_factory=list)

    def add(self, key: str, value: float):
        self.values[key] = self.values.get(key, 0.0) + value

    def merge(self, other: LiteralProperties):
        for key, value in other.values.items():
            self.add(key, value)
        self.deferred.extend(other.deferred)
        self.functions.extend(other.functions)


def _read(row: dict, fields: tuple[str, ...], path: str, *, prefix: str = "") -> LiteralProperties:
    result = LiteralProperties()
    for key in fields:
        value = row.get(prefix + key)
        if value not in (None, ""):
            result.add(key, float(value))

    def read_node(node, origin):
        if not isinstance(node, dict):
            return
        if node.get("__type__") == "sequence":
            for index, item in enumerate(node["items"]):
                read_node(item, f"{origin}[{index}]")
        elif node.get("__type__") == "hash":
            key, value = node["name"], node["value"]
            if isinstance(value, (int, float)):
                result.add(key, float(value))
            elif key == "Fun":
                result.deferred.append(origin)
                result.functions.extend(value if isinstance(value, list) else [value])
            # Non-numeric identity properties (ConnectArtifact/AntiList/etc.)
            # belong to the full property model, not this numeric stage.
        elif "__unparsed__" in node:
            result.deferred.append(origin)

    for key, node in row.get("Dynamic", {}).items():
        if key.startswith(prefix + "DynamicField"):
            read_node(node, f"{path}.{key}")
    return result


def _imprint(role: dict, tables: dict, *, self_imprint: bool) -> LiteralProperties:
    level = int(role.get("ImprintLV") or 0)
    if level < 1 or enabled(role.get("IsSelfImprint")) != self_imprint:
        return LiteralProperties()
    definition = tables["Role"][role["StaticID"]]
    identifier = definition.get("SelfImprint" if self_imprint else "TeamImprint")
    if not identifier:
        return LiteralProperties()
    row = tables["RoleImprint"].get(identifier)
    if row is None:
        raise GameError(f"缺少角色烙印属性：{identifier}")
    result = _read(row, (), f"RoleImprint.{identifier}", prefix="Base.")
    addition = _read(row, (), f"RoleImprint.{identifier}", prefix="LevelAdd.")
    for _ in range(level - 1):
        result.merge(addition)
    return result


def artifact_properties(artifact: dict, tables: dict) -> LiteralProperties:
    identifier = artifact["StaticID"]
    row = tables["Artifact"].get(identifier)
    if row is None:
        raise GameError(f"缺少神器属性：{identifier}")
    level = int(artifact.get("LV", 1))
    base = _read(row, ("HPValue", "AttackValue"), f"Artifact.{identifier}", prefix="Base.")
    maximum = _read(row, ("HPValue", "AttackValue"), f"Artifact.{identifier}", prefix="Max.")
    if level == MAX_ARTIFACT_LEVEL:
        result = maximum
        result.deferred.extend(base.deferred)
        result.functions = list(base.functions)
    else:
        rate = float32(float32(level - 1) / float32(MAX_ARTIFACT_LEVEL - 1))
        result = base
        for key in maximum.values.keys() | base.values.keys():
            initial = base.values.get(key, 0.0)
            result.values[key] = initial + (maximum.values.get(key, 0.0) - initial) * rate
        result.deferred.extend(maximum.deferred)
    increment = _read(row, (), f"Artifact.{identifier}", prefix="LVProp.")
    for _ in range(int(level / 3)):
        result.merge(increment)
    return result


def _add_converter(destination, addition):
    """SkillValueConvert.AddValue; only existing nested objects are enhanced."""
    for key in ("Base", "Rate", "ExtraRatePlus", "FinalMinValue"):
        destination[key] = float32(float(destination.get(key, 0) or 0) + float(addition.get(key, 0) or 0))
    # The recovered native method doubles its own MaxValue (s3+s3).
    destination["MaxValue"] = float32(2 * float(destination.get("MaxValue", 9999999)))
    for key in ("MaxValueConvert", "ValueConvert", "ExtraRate"):
        if destination.get(key) and addition.get(key):
            _add_converter(destination[key], addition[key])
    for existing, increment in zip(destination.get("ConditionRates") or [], addition.get("ConditionRates") or []):
        existing["Rate"] = float32(float(existing.get("Rate", 0)) + float(increment.get("Rate", 0)))


def _add_function(destination, addition):
    for key in ("Value", "UIRate", "AddCount", "StatusRound", "IgnoreResist", "CD"):
        if key in addition:
            destination[key] = float32(float(destination.get(key, 0) or 0) + float(addition[key] or 0))
    if float(destination.get("Rate", 1)) != 1:
        destination["Rate"] = float32(float(destination.get("Rate", 1)) + float(addition.get("Rate", 1)))
    for key, value in destination.items():
        if isinstance(value, dict) and isinstance(addition.get(key), dict) and key not in {"Condition", "PreCond", "SelectInfo", "FromSelect"}:
            _add_converter(value, addition[key])


def combine_functions(functions, providers=None):
    """Native ID priority, AddValue enhancement and highest-Value GID filter."""
    providers = providers or {}
    result, slots, additions = [], {}, []

    def wins(new, old):
        new_level, old_level = int(new.get("LV", 0) or 0), int(old.get("LV", 0) or 0)
        if new_level != old_level:
            return new_level > old_level
        for key in ("ValueConvert", "DefenceRate"):
            if new.get(key) is not None and old.get(key) is not None:
                # Native returns immediately here, including equal rates.
                return float(new[key].get("Rate", 0)) > float(old[key].get("Rate", 0))
        new_value, old_value = float(new.get("Value", 0)), float(old.get("Value", 0))
        if new_value != old_value:
            return new_value > old_value
        if new.get("_AuraSourceID") is not None and old.get("_AuraSourceID") is not None:
            return position(providers[new["_AuraSourceID"]]) < position(providers[old["_AuraSourceID"]])
        return False

    for original in functions:
        fun = deepcopy(original)
        if fun.get("Fun") == "AddValue":
            additions.append(fun)
            continue
        identifier = fun.get("ID")
        if identifier and identifier in slots:
            index = slots[identifier]
            if wins(fun, result[index]):
                result[index] = fun
        else:
            if identifier:
                slots[identifier] = len(result)
            result.append(fun)
    for addition in additions:
        if addition.get("ID") in slots:
            _add_function(result[slots[addition["ID"]]], addition)
    # Group filtering occurs after ID merging and enhancements, stable ties.
    groups = {}
    for fun in result:
        gid = fun.get("GID")
        if gid and (gid not in groups or float(fun.get("Value", 0)) > float(groups[gid].get("Value", 0))):
            groups[gid] = fun
    return [fun for fun in result if not fun.get("GID") or groups[fun["GID"]] is fun]


def _skill_level_rows(unit, skill, tables):
    definition = tables["Skill"].get(skill["StaticID"], {})
    reference = definition.get("ReferanceSkillID")
    record = next((s for s in (unit.get("Skills") or {}).get("Skills", []) if s.get("StaticID") == reference), skill)
    level_id = definition.get("LevelSkillID") or reference or skill["StaticID"]
    return [tables["SkillLevel"].get(level_id, {}).get(str(level), {}) for level in range(2, int(record.get("Level") or 1) + 1)]


def max_skill_level(sid, tables):
    """AllLevelStaticDataList stops at the first missing level, starting at 2."""
    row = tables["Skill"].get(sid, {})
    levels = tables["SkillLevel"].get(row.get("LevelSkillID") or row.get("ReferanceSkillID") or sid, {})
    level = 1
    while str(level + 1) in levels:
        level += 1
    return level


def _skill_setting(unit, skill, tables, *, passive=False):
    sid = skill["StaticID"]
    static = tables["Skill"].get(sid, {})
    prefix = "PassiveProp." if passive else ""
    result = _read(static, (), "Skill." + sid, prefix=prefix)
    for row in _skill_level_rows(unit, skill, tables):
        result.merge(_read(row, (), "SkillLevel." + sid, prefix=prefix))
    role = tables["Role"].get(unit["StaticID"], {})
    direct = direct_properties(role.get("Dynamic", {})).get("DirectAwakenSkill")
    awaken_id = direct or unit["StaticID"]
    rows = tables["RoleAwaken"].get(awaken_id, {})
    limit = max((int(i) for i in rows), default=0) if direct else int(unit.get("AwakenLV") or 0)
    enhancements = [row for level, row in rows.items() if int(level) <= limit and row.get("EnhanceSkill") in {sid, static.get("ReferanceSkillID")} and row.get("EnhanceSkill")]
    if enhancements:
        result.merge(_read(enhancements[-1], (), "RoleAwaken." + awaken_id, prefix="PassiveProp." if passive else "SkillProp."))
    result.functions = combine_functions(result.functions)
    return result


def _artifact_can_use(unit, team, tables):
    artifact = unit.get("ArtifactData") or {}
    if not enabled(tables["Artifact"].get(artifact.get("StaticID"), {}).get("IsFunSingle")):
        return True
    matching = [other for other in team if (other.get("ArtifactData") or {}).get("StaticID") == artifact.get("StaticID")]
    best = max((int(other["ArtifactData"].get("LV", 1)) for other in matching), default=1)
    return int(artifact.get("LV", 1)) == best and same(next(other for other in matching if int(other["ArtifactData"].get("LV", 1)) == best), unit)


def battle_role_properties(unit, team, tables):
    result = role_properties(unit, tables)
    # Formation has its own immutable base; statuses can temporarily seal a passive.
    formation = formation_properties(unit, position(unit), {u["id"]: u for u in team}, tables)
    result.merge(formation)
    if not _artifact_can_use(unit, team, tables):
        artifact = artifact_properties(unit["ArtifactData"], tables)
        # Numeric artifact stats still apply; only its executable functions stop.
        for fun in artifact.functions:
            result.functions.remove(fun)
    seal = float(status_properties(unit, tables).get("SealPassive", 0) or 0) > 0
    for skill in (unit.get("Skills") or {}).get("Skills", []):
        if tables["Skill"].get(skill["StaticID"], {}).get("SkillType") == "Passive":
            if seal:
                continue
            result.merge(_skill_setting(unit, skill, tables))
        result.merge(_skill_setting(unit, skill, tables, passive=True))
    return result


def _status_functions(unit, tables):
    result = []
    for status in unit.get("NowStatusList") or []:
        row = tables["Status"].get(status.get("StaticID"), {})
        # PropertySetting.MultiplyValue scales numeric properties, not Fun objects.
        result.extend(_read(row, (), "Status." + str(status.get("StaticID"))).functions)
    return result


def refresh_abilities(units, tables, *, round_role=None, rng=None, issues=None, initialize=False):
    """Reset abilities for one active wave using current, causally received state.

    HP/SP/cooldowns/statuses from trusted server events remain authoritative.
    Initial HP is filled only before events, after the property callback finishes.
    """
    issues = issues if issues is not None else set()
    providers = {u["id"]: u for u in units}
    base, functions = {}, {}
    for unit in units:
        team = [u for u in units if camp(u) == camp(unit)]
        props = battle_role_properties(unit, team, tables)
        for key, value in status_properties(unit, tables).items():
            if isinstance(value, (int, float)):
                props.add(key, value)
        base[unit["id"]] = props.values
        functions[unit["id"]] = combine_functions(props.functions + _status_functions(unit, tables))
        if initialize:
            _store_ability(unit, final_literal_values(props.values, injury_rate=float(unit.get("NowInjuryRate", 0))), props.values)
            unit["NowHP"] = unit["MaxHP"]
    updated = []
    for unit in units:
        own = camp(unit)
        allies, enemies = [u for u in units if camp(u) == own], [u for u in units if camp(u) != own]
        candidates = [{**fun, "_AuraSourceID": unit["id"]} for fun in functions[unit["id"]]
                      if not (enabled(fun.get("IsAura")) and enabled(fun.get("NoSelf")))]
        for other in units:
            if same(unit, other) or not alive(other):
                continue
            candidates.extend({**fun, "_AuraSourceID": other["id"]} for fun in functions[other["id"]]
                              if enabled(fun.get("IsAura")) and (camp(other) == own) == enabled(fun.get("AuraToAlly", True)))
        values = dict(base[unit["id"]])
        for fun in combine_functions(candidates, providers):
            if fun.get("Trigger") != "Property" or fun.get("Property") in {None, "", "None"}:
                continue
            aura = providers[fun["_AuraSourceID"]]
            if enabled(fun.get("IsAura")) and enabled(fun.get("NoSelf")) and same(unit, aura):
                continue
            kwargs = {"tables": tables, "round_role": round_role, "aura_source": aura, "issues": issues}
            if rng is not None:
                kwargs["rng"] = rng
            ctx = Context(unit, allies, enemies, target=unit, **kwargs)
            try:
                if fun.get("Condition") and not check(fun["Condition"], ctx):
                    continue
                if not all(check(c, ctx) for c in fun.get("Conditions") or []):
                    continue
                contribution = value_convert(fun["ValueConvert"], ctx) if fun.get("ValueConvert") else float32(float(fun.get("Value", 0) or 0))
                prop = fun["Property"]
                values[prop] = values.get(prop, 0) + float32(contribution)
            except EvaluationError as exc:
                issues.add(str(exc))
        updated.append((unit, final_literal_values(values, injury_rate=float(unit.get("NowInjuryRate", 0) or 0)), values))
    for unit, final, values in updated:
        _store_ability(unit, final, values)
        if initialize:
            unit["NowHP"] = unit["MaxHP"]
    return issues


def _store_ability(unit, final, values):
    unit["_AbilityProperties"] = {**values, **final}
    unit["NoInjuryMaxHP"] = final["HP"]
    unit["MaxHP"] = unit.get("_ServerMaxHP", final["InjuredHP"])
    for key in ("Attack", "Defence", "Speed"):
        unit[key] = final[key]


def role_properties(role: dict, tables: dict) -> LiteralProperties:
    """RoleData.CreateRolePropertySetting, excluding executable Fun values."""
    identifier = role["StaticID"]
    definition = tables["Role"].get(identifier)
    if definition is None:
        raise GameError(f"缺少角色属性：{identifier}")
    property_id, level = definition.get("RolePropertyID"), str(role.get("LV") or 1)
    level_row = tables["RoleProperty"].get(property_id, {}).get(level)
    if level_row is None:
        raise GameError(f"缺少角色等级属性：{identifier} / {level}")
    result = _read(level_row, BASE_STATS, f"RoleProperty.{property_id}.{level}")
    multipliers = _read(definition, BASE_STATS, f"Role.{identifier}")
    # PropertySetting.MultiplyValue(PropertySetting) converts the multiplier
    # to float32, while keeping the accumulated value in double precision.
    for key in result.values.keys() & multipliers.values.keys():
        result.values[key] *= float32(multipliers.values[key])
    result.deferred.extend(multipliers.deferred)
    result.functions.extend(multipliers.functions)
    variable = tables.get("PublicVariable", {}).get("RoleStarRate", {}).get("VALUE")
    star_rate = float32(float(variable)) if variable not in (None, "") else ROLE_STAR_RATE
    star_factor = float32(1 - float32(float32(MAX_STAR - int(role.get("Star") or 0)) * star_rate))
    for key in ("HP", "Attack", "Defence"):
        if key in result.values:
            result.values[key] *= star_factor

    equipment = list((role.get("EquipmentMap") or {}).values())
    for item in equipment:
        # EquipmentProperty: clone SubProps, then AddValue(MainProp).
        for prop in [*(item.get("SubProps", {}).get("SourceValues") or []), item.get("MainProp")]:
            if prop and prop.get("PropertyType"):
                result.add(prop["PropertyType"], float(prop.get("Value") or 0))
    for identifier, count in Counter(item.get("Set") for item in equipment if item.get("Set")).items():
        row = tables["EquipmentSet"].get(identifier)
        if row is None or int(row.get("Count") or 0) <= 0:
            raise GameError(f"缺少装备套装属性：{identifier}")
        addition = _read(row, SET_STATS, f"EquipmentSet.{identifier}")
        for _ in range(count // int(row["Count"])):
            result.merge(addition)
    if role.get("ArtifactData"):
        result.merge(artifact_properties(role["ArtifactData"], tables))
    result.merge(_imprint(role, tables, self_imprint=True))
    for awaken in range(1, int(role.get("AwakenLV") or 0) + 1):
        row = tables["RoleAwaken"].get(role["StaticID"], {}).get(str(awaken))
        if row is None:
            raise GameError(f"缺少角色觉醒属性：{role['StaticID']} / {awaken}")
        # SkillProp and PassiveProp are merged by their separate skill chain.
        result.merge(_read(row, AWAKEN_STATS, f"RoleAwaken.{role['StaticID']}.{awaken}"))
    return result


def formation_properties(role: dict, position: int, team: dict, tables: dict) -> LiteralProperties:
    result = LiteralProperties()
    for other in team.values():
        # Native callback excludes the same role instance, not the StaticID.
        if other.get("_id") == role.get("_id"):
            continue
        definition = tables["Role"].get(other["StaticID"], {})
        positions = [int(value) for value in definition.get("ImprintPositions", "").split("~") if value]
        if position in positions:
            result.merge(_imprint(other, tables, self_imprint=False))
    return result


def final_literal_values(values: dict, *, injury_rate: float = 0) -> dict:
    """Ability.CreateFinalValuePropertySetting + ResetTempValue numeric path.

    The caller must first supply function/aura contributions for final battle
    values. Calling this on role_properties alone yields an intermediate only.
    """
    result = dict(values)
    for name in ("HP", "Attack", "Defence", "Speed"):
        scale = float32(1 + float32(float(values.get(name + "Rate", 0))))
        final_scale = float32(1 + float32(float(values.get(name + "FinalRate", 0))))
        result[name] = (float(values.get(name, 0)) * scale + int(values.get(name + "Value", 0))) * final_scale
    # Convert.ToInt32(float) uses nearest-even; Attack/Defence use a cast.
    result["HP"] = round(float32(result["HP"]))
    result["InjuredHP"] = round(float32(float32(1 - float32(injury_rate)) * float32(result["HP"])))
    result["Attack"] = int(float32(result["Attack"]))
    result["Defence"] = int(float32(result["Defence"]))
    result["Speed"] = round(float32(result["Speed"]))
    return result
