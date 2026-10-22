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


def start_info(state: dict, sid: str, team_id: str, support: dict | None = None) -> dict:
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
        on_partial=None) -> dict:
    from .tasks import _build_support
    from .abyss_battle import run_activity_battle
    state = client.account_state
    if not re.fullmatch(r"BH\d+", family or ""):
        raise GameError("请选择当前活动")
    pending = getattr(client, "_activity_pending", None)
    if pending and pending[0] is state:
        raise GameError(f"关卡 {pending[1]} 结果未确认，等待登录同步刷新")
    initial = scenes(state, family)
    if not initial:
        raise GameError("当前活动没有开放的关卡")
    support = _build_support(support_entry) if support_entry else None
    # Validate all open stage references before spending resources on the first one.
    for stage in initial:
        if not stage["cleared"]:
            validate_scene(stage["id"])
    completed, lines, current = [], [], ""
    progress = {"family": family, "sceneId": "", "total": len(initial),
                "completed": sum(stage["cleared"] for stage in initial)}

    def publish(**extra):
        live = scenes(state, family)
        progress.update(sceneId=current, total=len(live), completed=sum(s["cleared"] for s in live), **extra)
        if on_progress:
            on_progress(deepcopy(progress))

    def result(ok, detail):
        publish(detail=detail, battle=None)
        return {"ok": ok, "detail": detail, "lines": lines, "completedScenes": completed,
                "lastSceneId": current}

    publish(detail="准备推图")
    while True:
        if cancel_event.is_set():
            return result(False, "自动推图已停止")
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
        info = start_info(state, current, team_id, support)
        publish(stageName=stage["name"], detail="挑战中", battle=None)
        if cancel_event.is_set():
            return result(False, "自动推图已停止")
        try:
            if stage["serverBattle"]:
                outcome = run_activity_battle(client, info, cancel_event=cancel_event,
                    on_progress=lambda battle: publish(stageName=stage["name"], detail="挑战中", battle=battle))
            else:
                outcome = finish_once(client, info, scene)
        except Exception as error:
            pending = getattr(client, "_activity_pending", None)
            suffix = "；结果未确认，等待登录同步刷新" if pending and pending[0] is state else ""
            return result(False, f"{stage['name']}：{error}{suffix}")
        if outcome.get("confirmed"):
            client._activity_pending = None
        if not outcome.get("ok") or not outcome.get("confirmed") or not cleared(records(state).get(current)):
            return result(False, f"{stage['name']}：{outcome.get('detail') or '结果未确认，等待登录同步刷新'}")
        completed.append(current)
        stage_lines = outcome.get("lines") or ["通关成功（结算未返回掉落）"]
        if on_partial is not None:
            # 复用账号日志流，下一关开始前即可显示；最终响应不重复这些日志。
            on_partial({"id": "activity_progress", "name": f"活动自动推图·{stage['name']}",
                        "ok": True, "lines": stage_lines, "detail": outcome.get("detail")})
        else:
            lines.extend(stage_lines)
        publish(stageName=stage["name"], detail="通关成功", battle=None)
        delay = config.get_request_delay_config()
        if delay.get("enabled"):
            ms = max(0, float(delay.get("ms") or 0) + random.uniform(-1, 1) * float(delay.get("jitter") or 0))
            if cancel_event.wait(ms / 1000):
                return result(False, "自动推图已停止")
