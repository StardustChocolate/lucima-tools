"""Realtime Fictional Abyss WebSocket runner.

The runner deliberately processes one text frame at a time.  It never keeps a
transcript or a growing response buffer in memory; only the latest protocol
state needed to choose the next action is retained.
"""
from __future__ import annotations

import asyncio
import json
import random
import ssl
import struct
from time import monotonic

import httpx

from . import config
from .game_client import GameError
from .logutil import log
from .tasks import _abyss_scene_records, _build_role_for_battle, _oid, _to_send_format
from .abyss_state import (enabled, fantasy_config, initialize_units, metadata as _battle_metadata,
                          status_properties, sync_units, sync_activity_support)
from .abyss_properties import refresh_abilities
from .abyss_rules import Context, EvaluationError, check, hp_rate, property_value, select


class BattleUnconfirmed(GameError):
    """Transport ended without a definitive result; synchronize before replay."""


class SupportUnavailable(GameError):
    """RoomServer explicitly rejected a used support before any action."""


SETTLEMENT_WAIT_SECONDS = 15


def check_battle_capacity(state: dict) -> None:
    """RoomServer refuses final settlement when an inventory is already full."""
    limits = state.get('AccountSaveData') or {}
    for label, key, container, rows in (
        ('角色', 'RolePackMaxCount', 'RoleDataContainer', 'Roles'),
        ('装备', 'EquipPackMaxCount', 'EquipmentContainer', 'Datas'),
        ('羁绊', 'ArtifactPackMaxCount', 'Artifacts', 'Datas'),
    ):
        limit = limits.get(key)
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            items = (state.get(container) or {}).get(rows) or []
            # EquipmentContainer/Artifacts include worn instances; those do
            # not occupy the unequipped-item backpack shown in the game.
            count = len(items) if container == 'RoleDataContainer' else sum(
                not _oid(item.get('EquipRole')) for item in items if isinstance(item, dict))
            if count >= limit:
                raise GameError(f'{label}背包已满（{count}/{limit}），服务器无法完成战斗结算。请先在游戏中清理空间，再开始挑战')


def _battle_server_error(value) -> str:
    text = str(value).strip()
    for key, label in (('Role backpack is full', '角色'), ('Equipment backpack is full', '装备'),
                       ('Equip backpack is full', '装备'), ('Artifact backpack is full', '羁绊')):
        if text.casefold() == key.casefold():
            return f'{label}背包已满，服务器拒绝本次战斗结算。请先在游戏中清理空间，再重新挑战'
    return '战斗服务器返回错误：' + text[:200]


def _ws_url(value) -> str:
    url = str(value or "").strip()
    if not url:
        raise GameError("建房响应没有提供 WebSocket 地址")
    if url.startswith("https://"):
        return "wss://" + url[8:]
    if url.startswith("http://"):
        return "ws://" + url[7:]
    if url.startswith(("ws://", "wss://")):
        return url
    return "wss://" + url.lstrip("/")


def _room_value(value):
    return _oid(value) if isinstance(value, dict) else value


def _scene(state: dict, scene_id: str) -> dict:
    for item in (state.get("SceneDataContainer", {}).get("Scenes", []) or []):
        if isinstance(item, dict) and str(item.get("StaticID")) == scene_id:
            return _to_send_format(item)
    first_attempt = _abyss_scene_records(state).get(scene_id)
    if first_attempt is not None:
        return _to_send_format(first_attempt)
    raise GameError("当前虚构深渊关卡未解锁或未开放，请刷新账号状态后重试")


def _wave_map(state: dict, ids: list) -> dict:
    result = {}
    for pos, role_id in enumerate(ids[:4]):
        if role_id:
            result[str(pos)] = _build_role_for_battle(state, str(role_id))
    return result


def _start_info(state: dict, scene_id: str, waves: dict) -> dict:
    return {
        "SceneData": _scene(state, scene_id),
        "CampData1": {}, "CampData2": {},
        "WaveCampDatas": [
            {"PositionRoleMap": _wave_map(state, waves.get("upper") or [])},
            {"PositionRoleMap": _wave_map(state, waves.get("lower") or [])},
        ],
        "IsRestart": 0, "Round": 0, "GM_Wave": 0,
        "IsRepeatAuto": 0, "BattleCountDown": -1, "IsNPCPVP": 0,
    }


_AI_RNG = random.Random()


def _skill_static(skill_id: str) -> dict:
    return dict(_battle_metadata()["tables"]["Skill"].get(str(skill_id)) or {})


def _live_skills(source: dict) -> list:
    """GetAllSkills: replacement IDs select existing, levelled skill records."""
    skills = [skill for skill in (source.get("Skills") or {}).get("Skills", []) if isinstance(skill, dict)]
    replacements = status_properties(source).get("AllSkillReplace")
    if not replacements:
        return skills
    identifiers = replacements if isinstance(replacements, list) else str(replacements).split("~")
    return [next((skill for skill in skills if skill.get("StaticID") == identifier), None) for identifier in identifiers]


def _alive(unit: dict) -> bool:
    return not bool(unit.get("NowIsDieOut") or unit.get("IsDieOut") or unit.get("IsDead")) and float(unit.get("NowHP", 1) or 0) > 0


def _can_act(unit: dict) -> bool:
    """WillPassRound: Dizzy, Sleep and HideOut are distinct from silence."""
    return not _status_flag(unit, {"Dizzy", "IsDizzy", "Sleep", "IsSleep", "HideOut", "IsHideOut"})


def _cannot_use_skill(unit: dict) -> bool:
    """AbilityData.CanUseSkill blockers observed in Ark2.

    The native getter treats taunt, sleep and silence as skill blockers.  A
    normal attack is handled separately by ProcessAI, so callers still get a
    chance to fall back to it.
    """
    return _status_flag(unit, {"Taunt", "IsTaunt", "Sleep", "IsSleep", "Silence", "IsSilence", "IsSilenced"})


def _rate(unit: dict) -> float:
    return hp_rate(unit)


def _float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _has_status(unit: dict, *, debuff: bool | None = None, status_id: str | None = None) -> bool:
    statuses = unit.get("NowStatusList") or []
    if status_id:
        return any(str(s.get("StaticID")) == status_id for s in statuses if isinstance(s, dict))
    if debuff is None:
        return bool(statuses)
    definitions = _battle_metadata()["tables"]["Status"]
    for item in statuses:
        if not isinstance(item, dict):
            continue
        kind = item.get("IsBuffType", definitions.get(str(item.get("StaticID")), {}).get("IsBuffType", "Debuff"))
        # Empty source enum is Debuff (0). 'None' is neither Buff nor Debuff.
        is_debuff = kind in ("", "Debuff", "DeBuff", 0)
        is_buff = kind in ("Buff", 1)
        if (debuff and is_debuff) or (not debuff and is_buff):
            return True
    return False


def _status_flag(unit: dict, names: set[str]) -> bool:
    for key in names:
        if unit.get(key):
            return True
    properties = {**unit.get("_AbilityProperties", {}), **status_properties(unit)}
    aliases = {"IsStealth": "Stealth", "IsHideOut": "HideOut", "IsInvincible": "Invincible",
               "IsDizzy": "Dizzy", "IsSleep": "Sleep", "IsTaunt": "Taunt", "IsSilence": "Silence", "IsSilenced": "Silence"}
    return any(float(properties.get(aliases.get(key, key), 0) or 0) > 0 for key in names)


_ELEMENT_CODE = {"Dark": 0, "Fire": 1, "Earth": 2, "Ice": 3, "Light": 4}


def _element_code(unit: dict) -> int | None:
    value = unit.get("Element") or unit.get("NowElement")
    if isinstance(value, int):
        return value
    if value is None:
        static_id = str(unit.get("StaticID") or "")
        value = _battle_metadata()["tables"]["Role"].get(static_id, {}).get("Element")
    if value is None:
        return None
    text = str(value)
    if text in _ELEMENT_CODE:
        return _ELEMENT_CODE[text]
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _is_conquer(attacker: dict, defender: dict) -> bool:
    """Exact RoleStaticData.IsConquer relation recovered from Ark2.

    Fire(1) beats Ice(3), Ice beats Earth(2), Earth beats Fire; Light(4) and
    Dark(0) beat each other. Unknown elements are deliberately excluded.
    """
    a, d = _element_code(attacker), _element_code(defender)
    if a is None or d is None:
        return False
    return (a == 1 and d == 3) or (a == 3 and d == 2) or (a == 2 and d == 1) \
        or (a == 4 and d == 0) or (a == 0 and d == 4)


def _compare(value: float, cond: dict) -> bool:
    try:
        target = float(cond.get("Value", 0) or 0)
        target2 = float(cond.get("Value2", 0) or 0)
    except (TypeError, ValueError):
        return False
    cmp = str(cond.get("Comparer") or "Eq")
    result = {"Eq": value == target, "Ne": value != target, "Gt": value >= target,
              "Gtn": value > target, "Lt": value <= target, "Ltn": value < target}.get(cmp)
    if cmp == "Between":
        result = min(target, target2) <= value <= max(target, target2)
    if result is None:
        return False
    return result


def _context(source, allies, enemies, *, target=None):
    return Context(source, allies, enemies, tables=_battle_metadata()["tables"], target=target,
                   round_role=source.get("_MainRoundRoleID", source.get("id")), rng=_AI_RNG)


def _record_issue(source, error):
    source["_EvaluationIssues"] = sorted(set(source.get("_EvaluationIssues") or []) | {str(error)})


def _condition_ok(cond: dict, source: dict, allies: list[dict], enemies: list[dict], *, target: dict | None = None) -> bool:
    try:
        return check(cond, _context(source, allies, enemies, target=target))
    except EvaluationError as exc:
        _record_issue(source, exc)
        return False


def _property_value(prop: str, unit: dict, source: dict, allies: list[dict], enemies: list[dict]) -> float:
    return property_value(prop, unit, _context(source, allies, enemies))


def _target(rule: dict, source_id: str, source: dict, allies: list[dict], enemies: list[dict], skill_info: dict | None = None) -> str | None:
    # Native CreateAction rebuilds selectors on every pass. The final pass
    # enables stealth alone; it doesn't inherit the preceding invincible flag.
    for force in (None, "IncludeInvincible", "IncludeStealth"):
        target = _target_pass(rule, source_id, source, allies, enemies, skill_info, force=force)
        if target:
            return target
    return None


def _target_pass(rule: dict, source_id: str, source: dict, allies: list[dict], enemies: list[dict], skill_info: dict | None = None, *, force: str | None = None) -> str | None:
    ctx = _context(source, allies, enemies)
    for setting in rule.get("Targets") or []:
        if not isinstance(setting, dict) or _AI_RNG.random() > float(setting.get("Rate", 1) or 0):
            continue
        info = {**setting, "TargetCamp": setting.get("TargetCamp") or rule.get("TargetCamp") or (skill_info or {}).get("TargetCamp") or "Enemy",
                "NoSelf": enabled(rule.get("NoSelf")) or enabled(setting.get("NoSelf")) or enabled((skill_info or {}).get("NoSelf")),
                "IncludeStealth": enabled(setting.get("IncludeStealth")),
                "IncludeInvincible": enabled(setting.get("IncludeInvincible"))}
        if force:
            info[force] = True
        for key in ("Property", "PropertyID", "TargetTypeID"):
            if key not in info and key in rule:
                info[key] = rule[key]
        try:
            candidates = select(info, ctx)
        except EvaluationError as exc:
            _record_issue(source, exc)
            continue
        if candidates:
            lowest = min(_rate(u) for u in candidates)
            return str(_AI_RNG.choice([u for u in candidates if _rate(u) == lowest])["id"])
    return None


def _skill_can_use(source: dict, skill: dict, allies: list[dict], enemies: list[dict], skill_info: dict | None = None) -> bool:
    """Mirror BattleSkillContainer.CanUseSkill's observable gates."""
    if not skill:
        return False
    sid = str(skill.get("StaticID") or "")
    if not sid:
        return False
    if _cannot_use_skill(source):
        skills = _live_skills(source)
        return bool(skills and skills[0] and sid == skills[0].get("StaticID"))
    try:
        cd = float((source.get("NowSkillCooldownMap") or {}).get(sid, 0) or 0)
    except (TypeError, ValueError):
        return False
    if cd > 0:
        return False
    # Native checks resource only when static SP is negative.
    try:
        raw_sp = skill.get("SP")
        if raw_sp is None and skill_info:
            raw_sp = skill_info.get("SP", 0)
        sp = float(raw_sp or 0)
        now_sp = float(source.get("NowSP", 0) or 0)
    except (TypeError, ValueError):
        return False
    if sp < 0 and now_sp + sp < 0:
        return False
    info = skill_info or skill
    pool = allies if str(info.get("TargetCamp") or "Enemy").lower() in {"my", "ally", "self"} else enemies
    include_dead = info.get("TargetType") in {"DeadOne", "AllWithDead", "OneWithDead"}
    return any((_alive(unit) or (include_dead and not unit.get("NowIsDieOut")))
               and not _status_flag(unit, {"HideOut", "IsHideOut"})
               and not (enabled(info.get("NoSelf")) and unit.get("id") == source.get("id")) for unit in pool)


def _choose_action(role_static: str, source_id: str, source: dict, allies: list[dict], enemies: list[dict], *, blocked_skills=()) -> tuple[str, int, str] | None:
    tables = _battle_metadata()["tables"]
    properties = status_properties(source)
    ai_ids = properties.get("AI") or (source.get("MonsterSetting") or {}).get("AIs") or tables["Role"].get(role_static, {}).get("AIs", "")
    def get_rules(value):
        identifiers = value if isinstance(value, list) else str(value).split("~")
        return sorted([rule for identifier in identifiers for rule in tables["AIAction"].get(identifier, [])],
                      key=lambda rule: -rule["Priority"])
    rules = get_rules(ai_ids)
    if properties.get("TauntAI"):
        rules = get_rules(properties["TauntAI"]) + rules
    for rule in rules:
        if any(not _condition_ok(c, source, allies, enemies) for c in rule.get("Conditions") or []):
            continue
        live_skills = _live_skills(source)
        # Ark2 indexes the live skill container directly, including form swaps.
        try:
            index = int(rule.get("SkillIndex"))
            live_skill = live_skills[-1 if index < 0 else index]
        except (TypeError, ValueError, IndexError):
            live_skill = None
        if live_skill is None or not live_skill.get("StaticID"):
            continue
        skill_id = str(live_skill["StaticID"])
        if skill_id in blocked_skills:
            continue
        static_skill = _skill_static(skill_id)
        skill_info = static_skill
        skill_for_check = {**static_skill, **live_skill}
        if not _skill_can_use(source, skill_for_check, allies, enemies, skill_info):
            continue
        target = _target(rule, source_id, source, allies, enemies, skill_info)
        if target:
            return skill_id, int(live_skill.get("Level") or 1), target
    return None


def _fallback_action(source: dict, allies: list[dict], enemies: list[dict]) -> tuple[str, int, str] | None:
    skills = _live_skills(source)
    normal = skills[0] if skills else None
    if normal is None:
        return None
    normal_id = str(normal.get("StaticID") or "")
    info = _skill_static(normal_id)
    if not _skill_can_use(source, {**info, **normal}, allies, enemies, info):
        return None
    if _status_flag(source, {"Taunt", "IsTaunt"}):
        target = _target_pass({"Targets": [{"Type": "TautSource", "IncludeStealth": True, "IncludeInvincible": True}]},
                              source["id"], source, allies, enemies, info)
        if target:
            return normal_id, int(normal.get("Level") or 1), target
    pool = allies if info.get("TargetCamp") == "My" else enemies
    candidates = [unit for unit in pool if _alive(unit) and not _status_flag(unit, {"HideOut", "IsHideOut"})
                  and not (enabled(info.get("NoSelf")) and unit.get("id") == source.get("id"))]
    if not candidates:
        return None

    def score(unit):
        # ProcessAI constants at 0xd8a970 and score closure 0x2103bb8.
        return (int(unit.get("NowHP") or 0) - 80000 * _is_conquer(source, unit)
                + 90000 * _is_conquer(unit, source)
                + 900000 * _status_flag(unit, {"Invincible", "IsInvincible"})
                + 9000000 * _status_flag(unit, {"Stealth", "IsStealth"}))

    return normal_id, int(normal.get("Level") or 1), str(min(candidates, key=score)["id"])


def _can_use_soul(source: dict, sid: str, soul: int, allies: list[dict], enemies: list[dict]) -> bool:
    info = _skill_static(sid)
    cost = int(info.get("SkillSoulCost") or 0)
    return cost > 0 and _skill_can_use(source, {**info, "StaticID": sid}, allies, enemies, info) \
        and (bool(status_properties(source).get("NoSoulCost")) or soul >= cost)


def _break_targets(enemies):
    from .abyss_team import status_group
    bosses=[unit for unit in enemies if _alive(unit) and unit.get('IsBoss')]
    return [unit for unit in bosses or enemies if _alive(unit) and not _has_status(unit,status_id='Immune')
            and not any(status_group(str(row.get('StaticID')or''))=='ReduceDef' for row in unit.get('NowStatusList')or[])
            and 'ReduceDef' not in str(status_properties(unit).get('AntiBuffGs')or'')]


def _soul_break_action(source,allies,enemies,soul,*,ignore_only=False):
    """Prefer a usable soul-burn armor-break skill; never bypass cooldown/SP/taunt."""
    from .abyss_team import skill_break,catalog
    try:catalog()
    except GameError as error:
        _record_issue(source,error)
        return None
    if _cannot_use_skill(source) or not _can_act(source):return None
    targets=_break_targets(enemies)
    if not targets:return None
    options=[]
    for skill in _live_skills(source):
        if not skill or not _can_use_soul(source,skill['StaticID'],soul,allies,enemies):continue
        if not skill_break(source,skill,soul=True):continue
        ignore=skill_break(source,skill,soul=True,ignore=True)
        if ignore_only and not ignore:continue
        options.append((ignore,skill))
    if not options:return None
    _,skill=max(options,key=lambda row:(row[0],int(_skill_static(row[1]['StaticID']).get('SkillSoulCost')or 0)))
    target=min(targets,key=lambda unit:(not unit.get('IsBoss',False),_rate(unit),str(unit['id'])))
    return skill['StaticID'],int(skill.get('Level')or 1),str(target['id'])


def _priority_debuff_action(source, allies, enemies, soul, *, soul_policy=None):
    """Apply missing boss counters before the ordinary damage AI."""
    from .abyss_team import role_traits, skill_status, skill_functions, walk, status_group
    if _cannot_use_skill(source) or not _can_act(source):return None
    bosses=[u for u in enemies if _alive(u) and u.get('IsBoss')]
    options=[]
    for target in bosses or enemies:
        if not _alive(target) or _status_flag(target,{'HideOut','IsHideOut'}):continue
        traits=role_traits(target)
        present={status_group(str(s.get('StaticID')or'')) for s in target.get('NowStatusList')or[] if isinstance(s,dict)}
        protections=bool(present & {'Shield','Invincible','Immortal','SkillNullifier'})
        immune=status_properties(target).get('AntiBuffGs')or[]
        immune=set(immune.split('~') if isinstance(immune,str) else immune)
        wanted={group for group,needed in (
            ('ReduceDef',True),('CannotBuff',traits['protection'] or protections),
            ('Unhealable',traits['recovery'])) if needed and group not in present and group not in immune}
        for skill in _live_skills(source):
            if not skill:continue
            sid=skill['StaticID'];definition=_skill_static(sid)
            if definition.get('SkillType')=='Passive' or not _skill_can_use(source,skill,allies,enemies,definition):continue
            if definition.get('TargetCamp')=='My':continue
            can_strip=any(n.get('Fun')=='RemoveBuf' for n in walk(skill_functions(source,skill)))
            # A dispel cast may remove Immunity before applying its own debuff.
            available=wanted if 'Immune' not in present or can_strip else set()
            use_soul=_can_use_soul(source,sid,soul,allies,enemies) and (soul_policy is None or soul_policy(sid))
            covered={group for group in available if skill_status(source,skill,group,soul=use_soul)}
            strip=bool(can_strip and (protections or 'Immune' in present))
            if not covered and not strip:continue
            bypass=sum(skill_status(source,skill,group,soul=True,ignore=True) for group in covered) if use_soul else 0
            key=(int(strip),len(covered & {'CannotBuff','Unhealable'}),int('ReduceDef' in covered),bypass,len(covered))
            options.append((key,sid,int(skill.get('Level')or 1),str(target['id'])))
    if not options:return None
    _,sid,level,target=max(options,key=lambda row:(row[0],row[1],row[3]))
    return sid,level,target


def _soul_allowed(source,sid,soul,allies,enemies,prioritize=False):
    if not _can_use_soul(source,sid,soul,allies,enemies):return False
    if status_properties(source).get('NoSoulCost'):return True
    if not prioritize or not _break_targets(enemies):return True
    from .abyss_team import skill_break,catalog
    try:catalog()
    except GameError:return True  # 旧手动配队仍可按已知冷却与星源正常烧魂。
    live=next((skill for skill in _live_skills(source) if skill and skill.get('StaticID')==sid),None)
    if live and skill_break(source,live,soul=True,ignore=True):return True
    own_cost=int(_skill_static(sid).get('SkillSoulCost')or 0)
    for ally in allies:
        if ally.get('id')==source.get('id') or not _alive(ally) or not _can_act(ally):continue
        for skill in _live_skills(ally):
            if skill and _can_use_soul(ally,skill['StaticID'],soul,allies,enemies) and skill_break(ally,skill,soul=True,ignore=True):
                reserve=int(_skill_static(skill['StaticID']).get('SkillSoulCost')or 0)
                if soul-own_cost<reserve:return False
    return True


def _elite_soul_allowed(source,sid,soul,allies,enemies):
    """Use boss soul for resistance-ignoring break first, then damage roles."""
    if not _can_use_soul(source,sid,soul,allies,enemies):return False
    from .abyss_team import catalog,combat_role,skill_break
    try:catalog()
    except GameError as error:
        _record_issue(source,error)
        return False
    targets=_break_targets(enemies)
    live=next((skill for skill in _live_skills(source) if skill and skill.get('StaticID')==sid),None)
    if targets and live and skill_break(source,live,soul=True,ignore=True):return True
    if combat_role(source)['kind'] not in ('single','area'):return False
    if status_properties(source).get('NoSoulCost'):return True
    if targets:
        for ally in allies:
            if not _alive(ally) or not _can_act(ally) or _cannot_use_skill(ally):continue
            for skill in _live_skills(ally):
                if not skill:continue
                definition=_skill_static(skill['StaticID'])
                # Reserve even before the balance reaches the break cost, so
                # cheaper damage burns cannot continually spend the savings.
                if (int(definition.get('SkillSoulCost')or 0)>0 and definition.get('SkillType')!='Passive'
                        and _skill_can_use(ally,{**definition,**skill},allies,enemies,definition)
                        and skill_break(ally,skill,soul=True,ignore=True)):
                    return False
    return True


def _progress_snapshot(scene_id: str, wave: int, units: dict[str, dict], *, single_team=False) -> dict:
    def members(camp):
        result = []
        for uid, unit in sorted(units.items(), key=lambda pair: int(pair[1].get("Position", 0))):
            member_wave = 0 if single_team and camp == 1 else wave
            if not uid.startswith("1-" if single_team and camp == 1 else f"{camp}-{member_wave}-"):
                continue
            dead = False if unit.get('_SupportHealthPending') else not _alive(unit)
            result.append({"id": uid, "staticId": unit.get("StaticID", ""),
                           "hpPercent": None if unit.get('_SupportHealthPending') else 0 if dead else round(max(0, min(1, hp_rate(unit))) * 100, 1),
                           "dead": dead, **({'support':True,'healthPending':bool(unit.get('_SupportHealthPending'))} if unit.get('_IsSupport') else {})})
        return result

    return {"sceneId": scene_id, "wave": wave, "enemies": members(2), "allies": members(1)}


def _battle_action(source, allies, enemies, soul, *, use_soul=False, prioritize_soul_break=False,
                   reserve_boss_tools=False, wave_index=0, wave_count=1):
    """Elite mobs use S1 except medics; boss waves unlock skills and soul."""
    bosses=[unit for unit in enemies if enabled(unit.get('IsBoss'))]
    if bosses:
        if _status_flag(source, {'Taunt','IsTaunt'}):
            forced=_target_pass({'Targets':[{'Type':'TautSource','IncludeStealth':True,'IncludeInvincible':True}]},
                                source['id'],source,allies,enemies,{})
            if forced not in {unit['id'] for unit in bosses}:
                return None,'taunted_by_add',False
        # Keep dead bosses in the pool: a confirmed boss kill must never make
        # the client switch to its surviving add while settlement is pending.
        enemies=bosses
    reserve = reserve_boss_tools and wave_index < wave_count - 1
    soul_enabled = use_soul and not reserve
    blocked = set()
    if reserve:
        definition=_battle_metadata()['tables']['Role'].get(str(source.get('StaticID')or''),{})
        if definition.get('Job')!='Medic':
            # Keep native live-container indices and form swaps intact. This
            # limits selected actions, not passive procs or automatic counters.
            blocked={skill['StaticID'] for skill in _live_skills(source)[1:] if skill}
    soul_checks={}
    def soul_allowed(sid):
        if sid not in soul_checks:
            soul_checks[sid]=(_elite_soul_allowed(source,sid,soul,allies,enemies) if reserve_boss_tools
                             else _soul_allowed(source,sid,soul,allies,enemies,prioritize_soul_break))
        return soul_checks[sid]
    choice = (_soul_break_action(source,allies,enemies,soul,ignore_only=reserve_boss_tools)
              if soul_enabled and prioritize_soul_break else None)
    decision = 'soul_armor_break' if choice else 'ai'
    if choice is None and prioritize_soul_break and not reserve:
        choice = _priority_debuff_action(source,allies,enemies,soul if soul_enabled else 0,
                                        soul_policy=soul_allowed if reserve_boss_tools else None)
        if choice:decision='boss_counter'
    if choice is None:
        args = (str(source.get('StaticID') or ''),source['id'],source,allies,enemies)
        choice = _choose_action(*args,blocked_skills=blocked) if blocked else _choose_action(*args)
    if choice is None:
        choice = _fallback_action(source,allies,enemies)
        decision = 'normal_attack'
    burn = bool(choice and soul_enabled and soul_allowed(choice[0]))
    return choice, decision, burn


async def _run(c, scene_id: str, waves: dict, *, use_soul: bool = False, cancel_event=None,
               on_progress=None, activity_start=None,prioritize_soul_break=False,reserve_boss_tools=False) -> dict:
    single_team = activity_start is not None
    label = "活动" if single_team else "虚构深渊"
    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise GameError(f"{label}战斗已中止，未自动重新开战")

    check_cancelled()
    try:
        from websockets.legacy.client import connect
        from websockets.exceptions import ConnectionClosed
    except ImportError as exc:
        raise GameError("缺少 websockets 依赖，请重新安装运行环境") from exc

    state = c.account_state or {}
    check_battle_capacity(state)
    start_info = activity_start if single_team else _start_info(state, scene_id, waves)
    if single_team:
        scene = _battle_metadata()["tables"]["Scene"][scene_id]
        fantasy = {"Waves": [{"Monsters": row["Ms"]} for row in scene["Waves"]]}
        units = initialize_units(start_info, fantasy, single_team=True)
    else:
        fantasy = fantasy_config(scene_id, state, now=getattr(c, "_battle_reference_utc", None))
        units = initialize_units(start_info, fantasy)
    wave_index = 0
    retired_units = set()
    selected_support=((start_info.get('Support')or{}).get('PlayerRoleData')or{}).get('RoleData')or{}
    support_info=({'staticId':selected_support.get('StaticID',''),'status':'configured'} if single_team and selected_support else None)
    turn_counter = None
    last_snapshot = None

    def publish_progress(*, phase="battle", message=None):
        nonlocal last_snapshot
        if on_progress is None:
            return
        snapshot = _progress_snapshot(scene_id, wave_index, units, single_team=single_team)
        if turn_counter is not None:
            snapshot['roundCount'] = turn_counter
        snapshot["phase"] = phase
        if support_info:snapshot['support']=dict(support_info)
        if message:
            snapshot["message"] = message
        if snapshot != last_snapshot:
            on_progress(snapshot)
            last_snapshot = snapshot

    publish_progress()
    started_at = monotonic()
    actions_sent = 0
    reported_issues = set()
    log.info("[abyss] start scene=%s data_version=%s use_soul=%s",
             scene_id, _battle_metadata()["version"], use_soul)
    check_cancelled()
    if single_team:
        c._activity_pending = (state, scene_id)
    room = c.call("RoomHandler.CreateRoomByScene", c._auth_data({"SceneID": scene_id}))
    room_id = _room_value(room.get("_id"))
    server_id = room.get("ServerID")
    aid = _room_value(room.get("AID") or c.aid)
    if room_id is None or server_id is None or aid is None:
        raise GameError("建房响应缺少房间 ID、ServerID 或 AID")
    url = _ws_url(room.get("Domain"))
    check_cancelled()
    headers = {"Origin": config.GAME_ORIGIN, "Referer": config.GAME_REFERER}
    # Match GameClient's certifi / SSL_CERT_FILE / SSL_CERT_DIR trust configuration.
    # The platform's default CA store can lack the RoomServer certificate's root.
    tls_options = {"ssl": httpx.create_ssl_context()} if url.startswith("wss://") else {}
    timeout = 20
    async with connect(url, extra_headers=headers, max_size=8 * 1024 * 1024, max_queue=4,
                       read_limit=64 * 1024, write_limit=64 * 1024,
                       compression=None, ping_interval=None, open_timeout=timeout, close_timeout=3, **tls_options) as ws:
        await ws.send(json.dumps({"ServerID": server_id}, separators=(",", ":")))
        client_index = client_oid = None
        sent_start = False
        sent_action = False
        sent_auth = False
        progress_at = monotonic()
        last_progress = None
        camp_souls = [0, 0]
        result = None
        reward = {}
        intimacy_synced = False
        ended_at = None
        reward_at = None
        settlement_at = None
        game_over = False
        while True:
            check_cancelled()
            if ended_at is not None and monotonic()-ended_at >= SETTLEMENT_WAIT_SECONDS:
                break
            if result is None and reward_at is not None and monotonic()-reward_at >= SETTLEMENT_WAIT_SECONDS:
                raise BattleUnconfirmed('已收到关卡奖励，但结束帧尚未确认，正在核对服务器进度')
            # This deadline cannot be renewed by alternating Step messages or
            # heartbeats after the last enemy died. HP alone never awards a win.
            if result is None and settlement_at is not None and monotonic()-settlement_at >= SETTLEMENT_WAIT_SECONDS:
                log.warning('[abyss] settlement_deadline scene=%s wave=%s game_over=%s', scene_id, wave_index, game_over)
                raise BattleUnconfirmed('战斗结束信号后未收到完整结算，正在核对服务器通关与星级')
            if monotonic() - progress_at >= (120 if sent_start else 30):
                raise BattleUnconfirmed(f"{label}战斗长时间未推进，连接已关闭")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                # A receive timeout isn't the settlement deadline. Keep this
                # socket alive while its result/reward counterpart is in flight.
                try:
                    await ws.send('{"HB":1}')
                except ConnectionClosed as error:
                    if result is not None:break
                    raise BattleUnconfirmed('等待战斗结算时连接断开') from error
                continue
            except ConnectionClosed as error:
                if result is not None:break
                raise BattleUnconfirmed('战斗连接在结算确认前断开') from error
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get('ErrorID') not in (None, '', 0, '0'):
                # This is a definitive rejection, not a lost result. Do not
                # hide it behind the settlement timeout or replay the battle.
                error = _battle_server_error(msg['ErrorID'])
                if single_team and actions_sent == 0 and result is None and ''.join(str(msg['ErrorID']).split()).casefold() == 'supportusetwice':
                    raise SupportUnavailable(error)
                raise GameError(error)
            # Ftimer is the stage counter. NowTurn counts individual actions
            # (including extra turns) and must not be used as the star counter.
            counter = (msg.get('RoundResult') or {}).get('TurnRoleTurn')
            if isinstance(counter, int) and not isinstance(counter, bool) and counter >= 0:
                turn_counter = counter if turn_counter is None else max(turn_counter, counter)
            if any(key in msg for key in ("Step", "CType", "NetID", "NetBattleGameOverCmd", "BattleEndData", "SceneRewardData")):
                signature = json.dumps({key: msg[key] for key in ("Step", "RoundResult", "ActionResult", "CType", "NetID", "NetBattleGameOverCmd", "BattleEndData", "SceneRewardData") if key in msg}, sort_keys=True)
                if signature != last_progress:
                    progress_at, last_progress = monotonic(), signature
            # Rewards and the final battle result can arrive in separate frames.
            # Merge a reward before sending an acknowledgement that may fail.
            if isinstance(msg.get('SceneRewardData'), dict):
                reward = msg['SceneRewardData']
                c.apply_scene_reward(reward)
                # SceneRewardData carries authoritative LineRoleDatas (friendship
                # level/exp) on every win. Keep the merge local instead of
                # re-logging the whole account after each battle.
                if isinstance(reward.get('LineRoleDatas'), list):
                    intimacy_synced = True
                if reward_at is None and isinstance(reward.get('SceneData'),dict) and reward['SceneData'].get('StaticID')==scene_id:
                    reward_at=monotonic()
            if isinstance(msg.get('BattleEndData'), dict):
                result = msg['BattleEndData']
                if ended_at is None:
                    ended_at = monotonic()
            # A final command can share a frame with a dead/controlled actor or
            # an empty target pool. Acknowledge before any AI early-return path;
            # the server may wait for this exact echo before sending settlement.
            if 'NetBattleGameOverCmd' in msg:
                game_over = True
                if settlement_at is None:
                    settlement_at = monotonic()
                try:
                    await ws.send(json.dumps(msg, separators=(',', ':')))
                    log.info('[abyss] end_ack scene=%s wave=%s result_received=%s', scene_id, wave_index, result is not None)
                except ConnectionClosed as error:
                    if result is None:
                        raise BattleUnconfirmed('结束回执发送时连接断开，结算尚未确认') from error
            if msg.get('HB') == 0:
                try:
                    await ws.send('{"HB":1}')
                except ConnectionClosed as error:
                    if result is not None:break
                    raise BattleUnconfirmed('等待战斗结算时连接断开') from error
            # The final frame may contain the last wave index and HP events.
            # Consume them before returning so farming can replace only the
            # half that actually lost, rather than guessing from the last UI.
            if single_team:
                retired_units.update(sync_activity_support(msg, units, start_info, fantasy)or())
                retired_units.difference_update(units)
                replacement=(msg.get('ActionResult')or{}).get('NewSupportRole')
                if isinstance(replacement,dict) and replacement.get('ID') in units:
                    support_info={'staticId':units[replacement['ID']].get('StaticID',''), 'status':'joined'}
                    log.info('[activity] support joined scene=%s role=%s unit=%s',scene_id,support_info['staticId'],replacement['ID'])
                elif support_info and support_info['status']=='configured' and sent_start and (msg.get('RoundResult')or{}).get('NowRoundRoleID'):
                    support_info['status']='reserved'
                    log.info('[activity] support configuration accepted scene=%s role=%s',scene_id,support_info['staticId'])
            sync_units(msg, units,ignored_ids=retired_units)
            round_result = msg.get("RoundResult") or {}
            if round_result.get("NowWaveIndex") in range(len(fantasy["Waves"])):
                wave_index = int(round_result["NowWaveIndex"])
            if result is not None:
                matched=isinstance(reward.get('SceneData'),dict) and reward['SceneData'].get('StaticID')==scene_id
                if str(result.get('Result')or'').lower()!='win' or matched:break
                continue
            final_enemies = [unit for uid, unit in units.items() if uid.startswith(f'2-{wave_index}-')]
            final_bosses=[unit for unit in final_enemies if enabled(unit.get('IsBoss'))]
            final_targets=final_bosses or final_enemies
            final_wave_defeated = ((not single_team or bool(final_bosses)) and sent_start and wave_index == len(fantasy['Waves'])-1
                                   and bool(final_targets) and all(not _alive(unit) for unit in final_targets))
            if game_over or final_wave_defeated:
                if settlement_at is None:
                    settlement_at = monotonic()
                    log.info('[abyss] awaiting_settlement scene=%s wave=%s reason=last_enemy_defeated', scene_id, wave_index)
                publish_progress(phase='settling', message='正在等待服务器结算并核对星级…' if not single_team else '正在等待服务器结算…')
            elif settlement_at is not None:
                # A server-confirmed revival means this was not the final kill.
                settlement_at = None
                publish_progress()
            elif "RoundResult" in msg or "ActionResult" in msg:
                publish_progress()
            souls = (msg.get("RoundResult") or {}).get("CampSoulList")
            if isinstance(souls, list) and len(souls) >= 2:
                camp_souls = list(souls[:2])
            if "NetID" in msg and not sent_auth:
                await ws.send(json.dumps({"RoomDBInfo": room_id, "AID": aid}, separators=(",", ":")))
                sent_auth = True
            if not sent_start and msg.get("CType") is not None and msg.get("Index") is not None:
                client_index, client_oid = msg.get("Index"), msg.get("OID")
                envelope = {"StartBattleInfo": start_info,
                            "Index": client_index, "OID": client_oid}
                await ws.send(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
                sent_start = True
            if game_over:
                continue
            step = msg.get("Step")
            if sent_start and step == "NowRoleBeforeRound" and not sent_action:
                rr = msg.get("RoundResult") or {}
                source = str(rr.get("NowRoundRoleID") or "")
                if source.startswith("1-"):
                    parts = source.split("-")
                    wave = parts[1] if len(parts) > 2 else "0"
                    pos = parts[2] if len(parts) > 2 else "0"
                    team_ids = (waves.get("upper") or []) if wave == "0" else (waves.get("lower") or [])
                    try:
                        role = team_ids[int(pos)]
                    except (IndexError, ValueError):
                        role = None
                    role_obj = (units.get(source) if single_team else
                                next((r for r in state.get("RoleDataContainer", {}).get("Roles", [])
                                      if _oid(r.get("_id")) == str(role)), None))
                    if role_obj:
                        enemy_wave = str(wave_index) if single_team else wave
                        active_units = [u for uid, u in units.items()
                                        if (single_team and uid.startswith("1-")) or uid.startswith(f"1-{wave}-") or uid.startswith(f"2-{enemy_wave}-")]
                        issues = set()
                        refresh_abilities(active_units, _battle_metadata()["tables"], round_role=source, rng=_AI_RNG, issues=issues)
                        for u in active_units:
                            issues.update(u.get("_EvaluationIssues") or [])
                        role_live = dict(role_obj)
                        role_live.update(units.get(source) or {})
                        role_live["_MainRoundRoleID"] = source
                        if not _alive(role_live) or not _can_act(role_live):
                            # Native battle engine emits an empty RoleAction for
                            # a controlled/dead actor; wait for that server
                            # acknowledgement before considering the next turn.
                            sent_action = True
                            log.info("[abyss] skip source=%s reason=controlled_or_dead", source)
                            continue
                        allies = [u for uid, u in units.items() if uid.startswith("1-" if single_team else f"1-{wave}-")]
                        enemies = [u for uid, u in units.items() if uid.startswith(f"2-{enemy_wave}-")]
                        choice, decision, burn = _battle_action(role_live,allies,enemies,int(camp_souls[0]),
                            use_soul=use_soul,prioritize_soul_break=prioritize_soul_break,
                            reserve_boss_tools=single_team and reserve_boss_tools,
                            wave_index=wave_index,wave_count=len(fantasy['Waves']))
                        issues.update(role_live.get("_EvaluationIssues") or [])
                        for issue in sorted(issues - reported_issues):
                            log.warning("[abyss] rule_unavailable source=%s detail=%s", source, issue)
                        reported_issues.update(issues)
                        if choice is None:
                            log.warning("[abyss] no_action source=%s role=%s", source, role_obj.get("StaticID"))
                            continue
                        skill, level, target = choice
                        action = {"SourceID": source, "TargetID": target,
                                  "SkillData": {"Level": level, "StaticID": skill},
                                  "IsUseSoul": int(burn),
                                  "Priority": 1, "ActionType": "Main",
                                  "ActionInfo": "None", "Index": client_index, "OID": client_oid}
                        await ws.send(json.dumps(action, separators=(",", ":")))
                        actions_sent += 1
                        log.info("[abyss] action source=%s skill=%s level=%s target=%s soul=%s decision=%s",
                                 source, skill, level, target, action["IsUseSoul"], decision)
                        sent_action = True
            if step == "RoleAction":
                sent_action = False
        if result is None:
            raise BattleUnconfirmed("WebSocket 在收到结算前断开")
    win = str((result or {}).get("Result") or "").lower() == "win"
    log.info("[abyss] end scene=%s result=%s actions=%s elapsed=%.1fs",
             scene_id, (result or {}).get("Result"), actions_sent, monotonic() - started_at)
    response = {"ok": win, "detail": f"{label}挑战完成" if win else f"{label}战斗失败", "raw": result or {},
                "settlementConfirmed":bool(isinstance(reward.get('SceneData'),dict) and reward['SceneData'].get('StaticID')==scene_id),
                "intimacySynced":intimacy_synced}
    if not single_team:
        from .abyss_result import resolve_stars
        stars, source = resolve_stars((reward.get('SceneData') or {}).get('Stars'), turn_counter, won=win)
        response.update(roundCount=turn_counter, stars=stars, starSource=source)
        # An Abyss loss ends at the current half. Later halves were not played;
        # entering the next half confirms the preceding half cleared.
        defeat = str((result or {}).get('Result') or '').lower() in {'lose', 'draw', 'defeat'}
        response['halfResults'] = {
            half: True if win or index < wave_index else False if defeat and index == wave_index else None
            for index, half in enumerate(('upper', 'lower'))}
    if single_team:
        if support_info:response['support']=support_info
        from .activity_progress import cleared
        returned = reward.get("SceneData") or {}
        response["confirmed"] = str(result.get("Result") or "").lower() in {"lose", "draw", "defeat"} or (
            win and returned.get("StaticID") == scene_id and cleared(returned))
        if win and not response["confirmed"]:
            response.update(ok=False, detail="结算结果未确认，等待登录同步刷新")
        if response["ok"] and response["confirmed"]:
            from .tasks import _format_scene_drop
            try:
                response["lines"] = _format_scene_drop(reward).get("lines", [])
            except (KeyError,TypeError,ValueError):
                log.exception('[activity] confirmed scene=%s drop display failed',scene_id)
                response['lines'] = ['通关已由服务器确认，部分掉落明细暂未显示']
    return response


def run_activity_battle(c, start_info: dict, *, cancel_event=None, on_progress=None,
                        use_soul=False, prioritize_soul_break=False,reserve_boss_tools=False) -> dict:
    try:
        return asyncio.run(_run(c, start_info["SceneData"]["StaticID"], {}, activity_start=start_info,
                                cancel_event=cancel_event, on_progress=on_progress,
                                use_soul=use_soul,prioritize_soul_break=prioritize_soul_break,
                                reserve_boss_tools=reserve_boss_tools))
    except ssl.SSLCertVerificationError as exc:
        raise GameError("战斗服务器证书验证失败，请检查网络或更新软件后重试") from exc


def run_abyss_battle(c, params: dict) -> dict:
    scene_id = str(params.get("sceneId") or "")
    waves = params.get("waves") or {}
    if not scene_id or not isinstance(waves, dict):
        raise GameError("虚构深渊战斗参数无效")
    from websockets.exceptions import WebSocketException
    try:
        return asyncio.run(_run(c, scene_id, waves, use_soul=enabled(params.get("useSoul")),
                                cancel_event=params.get("_cancel_event"), on_progress=params.get("_on_progress"),
                                prioritize_soul_break=enabled(params.get('prioritizeSoulBreak'))))
    except ssl.SSLCertVerificationError as exc:
        log.warning("[abyss] TLS verification failed scene=%s reason=%s", scene_id, exc.verify_message)
        raise GameError("战斗服务器证书验证失败，请检查网络或更新软件后重试") from exc
    except GameError as exc:
        log.warning("[abyss] stopped scene=%s reason=%s", scene_id, exc)
        raise
    except (WebSocketException, OSError, TimeoutError) as exc:
        log.warning('[abyss] transport_unconfirmed scene=%s type=%s', scene_id, type(exc).__name__)
        raise BattleUnconfirmed('战斗连接中断，正在核对服务器结算') from exc
