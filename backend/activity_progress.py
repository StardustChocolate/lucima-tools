"""Activity first clears: static catalog, per-scene teams and confirmed progression."""
from __future__ import annotations

from copy import deepcopy
import random
import re
import time
import uuid

from . import config
from .abyss_state import metadata
from .game_client import GameError, GameMessage
from .game_data import activity_open, enabled


SCENE_RE = re.compile(r"^(BH\d+)_(\d+)_(\d+)$")


def cleared(record: dict | None) -> bool:
    stars = (record or {}).get("Stars") or []
    return bool(stars and stars[0])


def records(state: dict) -> dict:
    return {row["StaticID"]: row for row in state.get("SceneDataContainer", {}).get("Scenes", [])
            if isinstance(row, dict) and row.get("StaticID")}


def retain_confirmed_clear(state: dict, record: dict) -> None:
    """A login used to return loans must not erase an acknowledged scene clear."""
    sid = record.get('StaticID')
    if not sid or not cleared(record):
        return
    rows = state.setdefault('SceneDataContainer', {}).setdefault('Scenes', [])
    current = next((row for row in rows if row.get('StaticID') == sid), None)
    if current is None:
        rows.append(deepcopy(record))
        return
    old, confirmed = current.get('Stars') or [], record.get('Stars') or []
    current['Stars'] = [int(bool((old[i] if i < len(old) else 0) or (confirmed[i] if i < len(confirmed) else 0)))
                        for i in range(max(len(old),len(confirmed)))]
    current['PassCount'] = max(int(current.get('PassCount') or 0),int(record.get('PassCount') or 0))


def scenes(state: dict, family: str = "", *, now=None) -> list[dict]:
    """All currently open stages, including stages never present in a login."""
    from .tasks import _activity_names, _localized_text, _ACTIVITY_TIERS
    tables = metadata()["tables"]
    now = time.time() if now is None else now
    saved, result = records(state), []
    for sid, scene in tables["Scene"].items():
        match = SCENE_RE.fullmatch(sid)
        if not match or (family and match[1] != family):
            continue
        if not all(activity_open(aid, state, now=now, tables=tables) for aid in
                   [scene.get("Chapter") or "Branch" + match[1][1:],
                    *filter(None, scene.get("ActivityIDs", "").split("~"))]):
            continue
        chapter, stage = int(match[2]), int(match[3])
        title = _localized_text(scene.get("Name", "")) or f"关卡{stage}"
        tier = _ACTIVITY_TIERS.get(stage)
        if tier and tier not in title:
            title += f"（{tier}）"
        result.append({"id": sid, "family": match[1], "chapter": chapter, "stage": stage,
                       "name": title, "activityName": _activity_names().get(match[1], match[1]),
                       "cleared": cleared(saved.get(sid)), "energy": int(scene.get("Energy") or 0),
                       "storyTeam": bool(scene.get("MyCampTeam")), "serverBattle": enabled(scene.get("ServerBattle")),
                       "preScene": scene.get("PreScene", "")})
    return sorted(result, key=lambda row: (row["family"], row["chapter"], row["stage"]))


def options(state: dict) -> list[dict]:
    groups = {}
    for scene in scenes(state):
        group = groups.setdefault(scene["family"], {"id": scene["family"], "name": scene["activityName"], "scenes": []})
        group["scenes"].append(scene)
    for group in groups.values():
        group["total"] = len(group["scenes"])
        group["completed"] = sum(scene["cleared"] for scene in group["scenes"])
    return list(groups.values())


def scene_record(state: dict, sid: str) -> dict:
    available = next((scene for scene in scenes(state) if scene["id"] == sid), None)
    if not available:
        raise GameError("活动关卡未开放，请刷新账号状态")
    prerequisite = available["preScene"]
    if prerequisite and not cleared(records(state).get(prerequisite)):
        raise GameError(f"前置关卡 {prerequisite} 尚未通关")
    return deepcopy(records(state).get(sid) or {"StaticID": sid, "Stars": [0, 0, 0], "PassCount": 0})


def validate_scene(sid: str) -> dict:
    tables = metadata()["tables"]
    scene = tables["Scene"].get(sid) or {}
    waves = scene.get("Waves") or []
    if not waves or any(not wave.get("Ms") for wave in waves):
        raise GameError(f"关卡 {sid} 的波次配置不完整，请更新资源")
    monsters = [m for wave in waves for m in wave["Ms"]]
    monsters += (scene.get("MyCampTeam") or {}).get("Ms", [])
    missing = [m.get("M") for m in monsters if m.get("M") not in tables["Role"]]
    if missing:
        raise GameError(f"关卡 {sid} 缺少角色配置：{'、'.join(sorted(set(missing)))}")
    return scene


def start_info(state: dict, sid: str, team_id: str, support: dict | None = None, *, role_positions=None) -> dict:
    from .tasks import _build_role_for_battle, _to_send_format
    scene = validate_scene(sid)
    story = (scene.get("MyCampTeam") or {}).get("Ms")
    if story:
        roster = {}
        for member in story:
            definition = metadata()["tables"]["Role"][member["M"]]
            roster[str(member.get("Pos", 1))] = {
                "_id": str(uuid.uuid4()), "StaticID": member["M"], "Exp": 0,
                "LV": int(member.get("LV") or 1), "AwakenLV": 0, "AwakenValue": 0,
                "Star": int(definition.get("InitStar") or 0), "ImprintLV": 0, "Locks": [],
                "IsLock": 0, "IsFavorite": 0, "IsSelfImprintOpen": 0,
                "IsDispatched": 0, "IsSelfImprint": 0,
                "Skills": {"Skills": [{"Level": 1, "StaticID": skill.split(":", 1)[0]}
                                      for skill in definition.get("SkillIDs", "").split("~") if skill]},
            }
    elif role_positions is not None:
        positions=list(role_positions.values())
        if not positions or len(positions)>4 or len(set(positions))!=len(positions) or any(pos not in range(4) for pos in positions):
            raise GameError('活动自动阵容站位无效')
        # The four owned heroes start first. Support is a reserve and enters
        # only when RoomServer emits NewSupportRole after an owned hero dies.
        roster={str(pos):_build_role_for_battle(state,rid) for rid,pos in role_positions.items()}
    else:
        team = next((t for t in state.get("Teams", {}).get("Settings", []) if str(t.get("ID")) == str(team_id)), None)
        role_map = (team or {}).get("TeamSetting", {}).get("RolePosMap") or {}
        if not role_map:
            raise GameError("所选队伍不存在或没有配置角色，请检查队伍设置")
        roster = {str(pos): _build_role_for_battle(state, role) for role, pos in role_map.items()}
    result = {"SceneData": _to_send_format(scene_record(state, sid)), "CampData1": {"PositionRoleMap": roster},
              "CampData2": {}, "IsRestart": 0, "Round": 0, "GM_Wave": 0, "IsRepeatAuto": 0}
    if support is not None and not story:
        result["Support"] = deepcopy(support)
    return {**result, "BattleCountDown": -1, "IsNPCPVP": 0}


def finish_once(client, info: dict, scene: dict) -> dict:
    from .tasks import _cache_urgent_missions, _format_scene_drop, _scene_failure_detail
    sid = info["SceneData"]["StaticID"]
    client._activity_pending = (client.account_state, sid)
    try:
        reward = client.call("SceneHandler.FinishScene", client._auth_data({
            "BattleEndData": {"StartBattleInfo": info,
                              "Camp2DeadList": [str(uuid.uuid4()) for _ in scene["Waves"][-1]["Ms"]],
                              "Result": "Win", "TurnRole": 0, "FinishWave": len(scene["Waves"]) - 1},
            "IsQuickBattle": 0}))
    except GameMessage as error:
        return {"ok": False, "confirmed": True, "detail": _scene_failure_detail(error.msg)}
    client.apply_scene_reward(reward)
    _cache_urgent_missions(client, reward)
    returned = reward.get("SceneData") or {}
    confirmed = returned.get("StaticID") == sid and cleared(returned)
    return {**(_format_scene_drop(reward) if confirmed else {}), "ok": confirmed, "confirmed": confirmed,
            "detail": "通关成功" if confirmed else "结算结果未确认，等待登录同步刷新"}


def run(client, family: str, team_id: str, support_entry=None, *, cancel_event, on_progress=None,
        on_partial=None, support_provider=None, account_key=None, account_lock=None, auto_sell=False,
        include_epic85=False) -> dict:
    from .tasks import _build_support
    from .abyss_battle import run_activity_battle
    state = client.account_state
    if not re.fullmatch(r"BH\d+", family or ""):
        raise GameError("请选择当前活动")
    pending = getattr(client, "_activity_pending", None)
    initial = scenes(state, family)
    if not initial:
        raise GameError("当前活动没有开放的关卡")
    support = _build_support(support_entry) if support_entry else None
    # Validate all open stage references before spending resources on the first one.
    for stage in initial:
        if not stage["cleared"]:
            validate_scene(stage["id"])
    completed, lines, current = [], [], ""
    elite_outcome = {}
    sales = {'enabled': auto_sell is True, 'sold': 0, 'rewardSummary': [], 'busy': False, 'error': ''}
    sale_rewards = {}
    sales_started = False
    progress = {"family": family, "sceneId": "", "total": len(initial),
                "completed": sum(stage["cleared"] for stage in initial)}

    def publish(**extra):
        live = scenes(client.account_state or {}, family)
        progress.update(sceneId=current, total=len(live), completed=sum(s["cleared"] for s in live), **extra)
        progress['equipmentSales'] = deepcopy(sales)
        if on_progress:
            on_progress(deepcopy(progress))

    def sell_equipment(reason):
        from . import abyss_loadout, equipment_sale
        if not sales['enabled'] or abyss_loadout.pending(client, account_key):
            return
        sales['busy'] = True
        publish(detail=f'自动贩卖·{reason}', battle=None)
        try:
            outcome = equipment_sale.sell(client, include_epic85=include_epic85 is True)
            sales['sold'] += outcome.get('sold') or 0
            for row in outcome.get('rewardSummary') or []:
                key = (row['kind'], row['id'])
                previous = sale_rewards.get(key, {})
                sale_rewards[key] = {**row, 'count': previous.get('count', 0) + row['count']}
            sales['rewardSummary'] = list(sale_rewards.values())
            if not outcome.get('ok'):
                sales.update(enabled=False, error=outcome.get('detail') or '服务器未确认贩卖结果')
            entry = {'id': 'equipment_sale', 'name': f'自动贩卖·{reason}', **outcome}
        except Exception as error:
            sales.update(enabled=False, error=f'自动贩卖已暂停：{error}')
            entry = {'id': 'equipment_sale', 'name': f'自动贩卖·{reason}', 'ok': False, 'detail': sales['error']}
        finally:
            sales['busy'] = False
        if on_partial:
            on_partial(entry)
        else:
            lines.append(f"[{entry['name']}] {entry.get('detail') or ''}")
        publish(battle=None)

    def result(ok, detail):
        nonlocal current
        if sales_started:
            if elite_outcome.get('recoveryRequired') and sales['enabled']:
                sales.update(enabled=False, error='装备与羁绊归还尚未确认，自动贩卖已暂停')
            sell_equipment('推图结束')
        if not current:
            current = next((row['id'] for row in reversed(scenes(client.account_state or {},family)) if row['cleared']), '')
        publish(detail=detail, battle=None)
        return {"ok": ok, "detail": detail, "lines": lines, "completedScenes": completed,
                "lastSceneId": current, "eliteAttempts":elite_outcome.get('attempts',[]),
                "progress":deepcopy(progress),
                "recoveryRequired":bool(elite_outcome.get('recoveryRequired'))}

    publish(detail="准备推图")
    if pending and pending[0] is state:
        publish(detail='正在自动同步上次挑战结果')
        try:client.bootstrap()
        except GameError as error:return result(False,'上次挑战结果暂时无法同步，请稍后再次开始：'+str(error))
    client._activity_pending = None
    if not cancel_event.is_set():
        sales_started = True
        sell_equipment('开始推图')
    while True:
        if cancel_event.is_set():
            return result(False, "自动推图已停止")
        state=client.account_state or {}
        live = scenes(state, family)
        if not live:
            return result(False, "活动已结束，自动推图已停止")
        todo = [stage for stage in live if not stage["cleared"]]
        if not todo:
            return result(True, "当前已开放关卡全部通关")
        saved = records(state)
        stage = next((s for s in todo if not s["preScene"] or cleared(saved.get(s["preScene"]))), None)
        if stage is None:
            return result(False, "剩余关卡的前置条件尚未满足")
        current = stage["id"]
        scene = validate_scene(current)
        items = {item.get("StaticID"): item for item in state.get("ItemContainer", {}).get("Items", [])}
        energy = items.get("3")  # CURRENCY_IDS.stamina
        if energy is not None and int(energy.get("Count") or 0) < stage["energy"]:
            return result(False, "体力不足，自动推图已停止")
        if scene.get("EnterCost"):
            return result(False, "此关卡有额外入场消耗，暂不支持自动推图")
        publish(stageName=stage["name"], detail="挑战中", battle=None)
        if cancel_event.is_set():
            return result(False, "自动推图已停止")
        try:
            if stage['serverBattle'] and stage['stage']==13 and not stage['storyTeam']:
                from .activity_elite import run as run_elite
                outcome=run_elite(client,current,cancel_event=cancel_event,
                    support_provider=support_provider,selected_support=support_entry,account_key=account_key,
                    account_lock=account_lock,on_progress=lambda value:publish(stageName=stage['name'],**value))
                elite_outcome=outcome
            elif stage["serverBattle"]:
                info = start_info(state, current, team_id, support)
                outcome = run_activity_battle(client, info, cancel_event=cancel_event,
                    on_progress=lambda battle: publish(stageName=stage["name"], detail="挑战中", battle=battle))
            else:
                info = start_info(state, current, team_id, support)
                outcome = finish_once(client, info, scene)
        except Exception as error:
            pending = getattr(client, "_activity_pending", None)
            suffix = "；结果未确认，等待登录同步刷新" if pending and pending[0] is state else ""
            return result(False, f"{stage['name']}：{error}{suffix}")
        if outcome.get("confirmed"):
            client._activity_pending = None
        if not outcome.get("ok") or not outcome.get("confirmed") or not cleared(records(client.account_state or {}).get(current)):
            return result(False, f"{stage['name']}：{outcome.get('detail') or '结果未确认，等待登录同步刷新'}")
        completed.append(current)
        stage_lines = outcome.get("lines") or ["通关成功（结算未返回掉落）"]
        if on_partial is not None:
            # 复用账号日志流，下一关开始前即可显示；最终响应不重复这些日志。
            on_partial({"id": "activity_progress", "name": f"活动自动推图·{stage['name']}",
                        "ok": True, "lines": stage_lines, "detail": outcome.get("detail")})
        else:
            lines.extend(stage_lines)
        if len(completed) % 10 == 0 and not cancel_event.is_set():
            sell_equipment('完成 10 关')
        publish(stageName=stage["name"], detail="通关成功", battle=None)
        delay = config.get_request_delay_config()
        if delay.get("enabled"):
            ms = max(0, float(delay.get("ms") or 0) + random.uniform(-1, 1) * float(delay.get("jitter") or 0))
            if cancel_event.wait(ms / 1000):
                return result(False, "自动推图已停止")
