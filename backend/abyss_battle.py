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


def _choose_action(role_static: str, source_id: str, source: dict, allies: list[dict], enemies: list[dict]) -> tuple[str, int, str] | None:
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


def _progress_snapshot(scene_id: str, wave: int, units: dict[str, dict], *, single_team=False) -> dict:
    def members(camp):
        result = []
        for uid, unit in sorted(units.items(), key=lambda pair: int(pair[1].get("Position", 0))):
            member_wave = 0 if single_team and camp == 1 else wave
            if not uid.startswith("1-" if single_team and camp == 1 else f"{camp}-{member_wave}-"):
                continue
            dead = not _alive(unit)
            result.append({"id": uid, "staticId": unit.get("StaticID", ""),
                           "hpPercent": 0 if dead else round(max(0, min(1, hp_rate(unit))) * 100, 1),
                           "dead": dead})
        return result

    return {"sceneId": scene_id, "wave": wave, "enemies": members(2), "allies": members(1)}


async def _run(c, scene_id: str, waves: dict, *, use_soul: bool = False, cancel_event=None,
               on_progress=None, activity_start=None) -> dict:
    single_team = activity_start is not None
    label = "活动" if single_team else "虚构深渊"
    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise GameError(f"{label}战斗已中止，未自动重新开战")

    check_cancelled()
    try:
        from websockets.legacy.client import connect
    except ImportError as exc:
        raise GameError("缺少 websockets 依赖，请重新安装运行环境") from exc

    state = c.account_state or {}
    start_info = activity_start if single_team else _start_info(state, scene_id, waves)
    if single_team:
        scene = _battle_metadata()["tables"]["Scene"][scene_id]
        fantasy = {"Waves": [{"Monsters": row["Ms"]} for row in scene["Waves"]]}
        units = initialize_units(start_info, fantasy, single_team=True)
    else:
        fantasy = fantasy_config(scene_id, state, now=getattr(c, "_battle_reference_utc", None))
        units = initialize_units(start_info, fantasy)
    wave_index = 0
    last_snapshot = None

    def publish_progress():
        nonlocal last_snapshot
        if on_progress is None:
            return
        snapshot = _progress_snapshot(scene_id, wave_index, units, single_team=single_team)
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
                       compression=None, ping_interval=None, open_timeout=timeout, **tls_options) as ws:
        await ws.send(json.dumps({"ServerID": server_id}, separators=(",", ":")))
        client_index = client_oid = None
        sent_start = False
        sent_action = False
        sent_auth = False
        progress_at = monotonic()
        last_progress = None
        camp_souls = [0, 0]
        result = None
        while True:
            check_cancelled()
            if monotonic() - progress_at >= (120 if sent_start else 30):
                raise GameError(f"{label}战斗长时间未推进，连接已关闭；请刷新状态确认结果")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                await ws.send('{"HB":1}')
                continue
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            if any(key in msg for key in ("Step", "CType", "NetID", "NetBattleGameOverCmd", "BattleEndData")):
                signature = json.dumps({key: msg[key] for key in ("Step", "RoundResult", "ActionResult", "CType", "NetID", "BattleEndData") if key in msg}, sort_keys=True)
                if signature != last_progress:
                    progress_at, last_progress = monotonic(), signature
            if single_team:
                sync_activity_support(msg, units, start_info, fantasy)
            sync_units(msg, units)
            round_result = msg.get("RoundResult") or {}
            if round_result.get("NowWaveIndex") in range(len(fantasy["Waves"])):
                wave_index = int(round_result["NowWaveIndex"])
            if "RoundResult" in msg or "ActionResult" in msg:
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
            if msg.get("HB") == 0:
                await ws.send('{"HB":1}')
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
                        choice = _choose_action(str(role_obj.get("StaticID")), source, role_live, allies, enemies)
                        decision = "ai"
                        if choice is None:
                            choice = _fallback_action(role_live, allies, enemies)
                            decision = "normal_attack"
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
                                  "IsUseSoul": int(use_soul and _can_use_soul(role_live, skill, int(camp_souls[0]), allies, enemies)),
                                  "Priority": 1, "ActionType": "Main",
                                  "ActionInfo": "None", "Index": client_index, "OID": client_oid}
                        await ws.send(json.dumps(action, separators=(",", ":")))
                        actions_sent += 1
                        log.info("[abyss] action source=%s skill=%s level=%s target=%s soul=%s decision=%s",
                                 source, skill, level, target, action["IsUseSoul"], decision)
                        sent_action = True
            if step == "RoleAction":
                sent_action = False
            if "NetBattleGameOverCmd" in msg:
                await ws.send(json.dumps(msg, separators=(",", ":")))
            if "BattleEndData" in msg:
                result = msg.get("BattleEndData") or {}
                reward = msg.get("SceneRewardData") or {}
                if isinstance(msg.get("SceneRewardData"), dict):
                    c.apply_scene_reward(msg["SceneRewardData"])
                break
        if result is None:
            raise GameError("WebSocket 在收到结算前断开")
    win = str((result or {}).get("Result") or "").lower() == "win"
    log.info("[abyss] end scene=%s result=%s actions=%s elapsed=%.1fs",
             scene_id, (result or {}).get("Result"), actions_sent, monotonic() - started_at)
    response = {"ok": win, "detail": f"{label}挑战完成" if win else f"{label}战斗失败", "raw": result or {}}
    if single_team:
        from .activity_progress import cleared
        returned = reward.get("SceneData") or {}
        response["confirmed"] = str(result.get("Result") or "").lower() in {"lose", "draw", "defeat"} or (
            win and returned.get("StaticID") == scene_id and cleared(returned))
        if win and not response["confirmed"]:
            response.update(ok=False, detail="结算结果未确认，等待登录同步刷新")
        if response["ok"] and response["confirmed"]:
            from .tasks import _format_scene_drop
            response["lines"] = _format_scene_drop(reward).get("lines", [])
    return response


def run_activity_battle(c, start_info: dict, *, cancel_event=None, on_progress=None) -> dict:
    try:
        return asyncio.run(_run(c, start_info["SceneData"]["StaticID"], {}, activity_start=start_info,
                                cancel_event=cancel_event, on_progress=on_progress))
    except ssl.SSLCertVerificationError as exc:
        raise GameError("战斗服务器证书验证失败，请检查网络或更新软件后重试") from exc


def run_abyss_battle(c, params: dict) -> dict:
    scene_id = str(params.get("sceneId") or "")
    waves = params.get("waves") or {}
    if not scene_id or not isinstance(waves, dict):
        raise GameError("虚构深渊战斗参数无效")
    try:
        return asyncio.run(_run(c, scene_id, waves, use_soul=enabled(params.get("useSoul")),
                                cancel_event=params.get("_cancel_event"), on_progress=params.get("_on_progress")))
    except ssl.SSLCertVerificationError as exc:
        log.warning("[abyss] TLS verification failed scene=%s reason=%s", scene_id, exc.verify_message)
        raise GameError("战斗服务器证书验证失败，请检查网络或更新软件后重试") from exc
    except GameError as exc:
        log.warning("[abyss] stopped scene=%s reason=%s", scene_id, exc)
        raise
