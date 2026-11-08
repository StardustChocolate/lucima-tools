"""装备页 /api/equipment/action 的分派入口（appcore 只负责账号与锁）。"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Callable

from backend.game_client import GameError
from . import auto, plans, stats, search_jobs, bonds, recommend, library

WEB_DIR = Path(__file__).resolve().parent / "web"

SET_CN = {
    "Attack": "攻击", "Defense": "防御", "Health": "生命", "Speed": "速度", "Critical": "暴击",
    "Destruction": "暴伤", "Hit": "命中", "Resist": "抗性", "Lifesteal": "吸血", "Counter": "反击",
    "Pinch": "追击", "Immunity": "免疫", "Rage": "暴怒", "Penetration": "贯穿", "Revenge": "复仇",
    "Injury": "创伤", "Torrent": "飞流",
}
SET_DESC = {
    "Attack": "攻击力 +45%", "Defense": "防御力 +20%", "Health": "生命力 +20%", "Speed": "速度 +16%",
    "Critical": "暴击率 +12%", "Destruction": "暴击伤害 +50%", "Hit": "状态命中 +20%",
    "Resist": "状态抗性 +20%", "Lifesteal": "攻击时按伤害 20% 回复生命", "Counter": "受击时 30% 反击",
    "Pinch": "追击率 +8%，追击伤害 +6%", "Immunity": "战斗开始获得 1 回合免疫",
    "Rage": "目标有负面状态时伤害 +30%", "Penetration": "单体攻击无视 15% 防御",
    "Revenge": "速度 +12%，每损失 1% 生命再 +0.5%", "Injury": "攻击后削减目标最大生命（最多 6%/单体 12%）",
    "Torrent": "生命力 -10%，伤害 +10%",
}


def _meta() -> dict:
    sets = [{"id": k, "name": SET_CN.get(k, k), "count": v["count"], "desc": SET_DESC.get(k, ""),
             "icon": f"/assets/sets/{'Riptide' if k == 'Torrent' else k}.png"}
            for k, v in stats.tables()["sets"].items()]
    sets.sort(key=lambda s: (-s["count"], s["name"]))
    return {"sets": sets,
            "stats": [{"id": s, "name": stats.STAT_CN[s], "percent": s in stats.PERCENT_STATS}
                      for s in stats.STATS]}


@lru_cache(maxsize=1)
def _aliases():
    return json.loads((WEB_DIR.parent / "role_aliases.json").read_text(encoding="utf-8"))


def _snapshot(client):
    state = client.account_state or {}
    return copy.deepcopy({k: state.get(k, {}) for k in ("EquipmentContainer", "RoleDataContainer", "Artifacts")})


def _fingerprint(client):
    return hashlib.sha256(json.dumps(_snapshot(client), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _validate_results(entries):
    used = set()
    for entry in entries:
        if not entry.get("results"): continue
        combo = entry["results"][entry["pick"]]
        ids = list(combo["equipIds"].values())
        if set(combo["equipIds"]) != set(plans.SLOTS) or len(set(ids)) != 6 or used.intersection(ids):
            raise auto.BuildError("配装出现部位缺失或装备重复，请重新计算")
        used.update(ids)


def handle(acc, action: str, body: dict, avatar: Callable[[str], str | None], *, _job=None) -> dict:
    client = acc.client

    # 进度/停止接口不获取账号锁；计算线程持有账号锁以保护本轮装备快照。
    if action in ("searchStatus","cancelSearch"):
        job = search_jobs.current(acc,body.get("searchId"))
        if action == "cancelSearch": job.stop()
        return job.view()
    if action in ("autoMulti","chooseMulti") and body.get("background") and _job is None:
        request = copy.deepcopy(body)
        request.pop("background",None)
        return search_jobs.start(acc,lambda job:handle(acc,action,request,avatar,_job=job))

    def excluded_ids():
        return list(getattr(acc, "equipment_excluded_roles", []))

    def excluded_roles():
        roles = {plans._oid(r.get("_id")): plans._role_view(r) for r in plans._roles(client)}
        return [{"id": rid, "name": roles.get(rid, {}).get("name") or rid} for rid in excluded_ids()]

    def overview():
        data = plans.overview(client)
        state = client.account_state or {}
        roles = {plans._oid(r.get("_id")): r for r in (state.get("RoleDataContainer") or {}).get("Roles") or []}
        equips = (state.get("EquipmentContainer") or {}).get("Datas") or []
        views = {e["id"]: e for e in data["equips"]}
        for equip in equips:
            view = views.get(plans._oid(equip.get('_id')))
            if view is not None:
                view['quality'] = library.quality(equip)
        data['library'] = library.overview(equips)
        data["artifacts"] = bonds.catalog(state)
        artifact_map = {art["id"]: art for art in data["artifacts"]}
        for role in data["roles"]:
            role["avatar"] = avatar(role["staticID"])
            alias = _aliases().get(role["staticID"], {})
            role["aliases"] = alias.get("aliases", [])
            role["searchName"] = alias.get("name", "")
            if role["id"] in roles:
                role["recommendations"] = recommend.profiles(roles[role["id"]])
                role["job"] = bonds.job(roles[role["id"]])
                worn_bonds = bonds.worn(state, role["id"])
                role["artifact"] = artifact_map.get(plans._oid(worn_bonds[-1].get("_id"))) if worn_bonds else None
                worn, conflicts = plans.worn_items(equips, role["id"])
                if stats.role_base(roles[role["id"]]) and all(plans.equip_slot(e) in plans.SLOTS for e in worn):
                    try:
                        if getattr(acc, "equipment_artifact_uncertain", False):
                            raise GameError("羁绊状态尚未同步，请重新进入装备页同步后再筛选")
                        role["current"] = {**stats.role_panel(state, roles[role["id"]], worn, include_artifact_effects=True),
                            "count": len(worn), "aux": sum(views[plans._oid(e.get("_id"))].get("auxScore", 0) or 0 for e in worn)}
                    except GameError as error:
                        role["currentWarning"] = str(error)
                    if conflicts:
                        role["currentWarning"] = "存在同部位重复归属，摘要按当前生效装备计算；卸装会移除该角色全部归属装备。"
                else:
                    role["currentWarning"] = "当前装备或角色资料不全，暂时无法计算完整属性摘要。"
        data.pop("planCount", None)
        data.update(_meta())
        data["artifactUncertain"] = bool(getattr(acc, "equipment_artifact_uncertain", False))
        data["excludedRoles"] = excluded_roles()
        return data

    def public(batch):
        return {"batchId": batch["id"], "entries": copy.deepcopy(batch["results"]),
                "configs": copy.deepcopy(batch["configs"]), "options": copy.deepcopy(batch["options"]),
                "excludedRoles": excluded_roles()}

    def get_batch():
        batch = getattr(acc, "equipment_batch", None)
        if not batch or body.get("batchId") != batch["id"]:
            raise GameError("配装任务已失效，请重新计算")
        if batch.get("uncertain") or batch["fingerprint"] != _fingerprint(client):
            raise GameError("装备或角色数据已变化，请同步账号并重新配装")
        if not batch.get("roleScopedExclusions") and batch["options"].get("excluded_roles", []) != excluded_ids():
            raise GameError("排除角色已变化，请重新计算配装")
        return batch

    def calculate(batch, prefix=None):
        state = batch["state"]
        roles = {plans._oid(r.get("_id")): r for r in (state.get("RoleDataContainer") or {}).get("Roles") or []}
        result = auto.search_multi(state, roles, batch["configs"], prefix=prefix,
                                   progress=_job.update if _job else None,
                                   cancelled=_job.cancel.is_set if _job else None, **batch["options"])
        if _job and _job.cancel.is_set(): raise auto.SearchCancelled("已停止配装计算，未返回未完成的方案")
        _validate_results(result)
        return result

    def build_multi():
        if getattr(acc, "equipment_artifact_uncertain", False):
            raise GameError("羁绊状态尚未同步，请重新进入装备页同步后再筛选")
        configs = body.get("entries")
        if not isinstance(configs, list) or not 1 <= len(configs) <= 6 or not all(isinstance(c, dict) for c in configs):
            raise GameError("请选择 1～6 个角色")
        configs = copy.deepcopy(configs)
        # 新客户端按目标角色传入独立名单；旧客户端保留原有轮次规则。
        additions = body.get("excludedRoleIds", [])
        known = {plans._oid(r.get("_id")) for r in plans._roles(client)}
        role_scoped = all("excludedRoleIds" in entry for entry in configs)
        for entry in configs:
            if "excludedRoleIds" not in entry:
                continue
            ids = entry["excludedRoleIds"]
            if not isinstance(ids, list) or any(not isinstance(rid, str) or rid not in known for rid in ids):
                raise GameError("角色排除名单包含无效角色，请重新选择")
            if entry.get("roleId") in ids:
                raise GameError("配装角色不能排除自己的装备")
            entry["excludedRoleIds"] = list(dict.fromkeys(ids))
        if not isinstance(additions, list) or any(not isinstance(rid, str) or rid not in known for rid in additions):
            raise GameError("排除名单包含无效角色，请同步账号后重新选择")
        protected = list(dict.fromkeys([*excluded_ids(), *additions]))
        if not role_scoped and protected != excluded_ids():
            acc.equipment_excluded_roles = protected
            acc.equipment_batch = None
        batch = {"id": uuid.uuid4().hex, "state": _snapshot(client), "fingerprint": _fingerprint(client),
                 "roleScopedExclusions": role_scoped,
                 "configs": copy.deepcopy(configs), "options": {"only_max": body.get("onlyMax", True) is not False,
                 "include_worn": bool(body.get("includeWorn")),"excluded_roles":[] if role_scoped else excluded_ids(),"complete":True}}
        batch["results"] = calculate(batch)
        acc.equipment_batch = batch
        return public(batch)

    def index_for(batch):
        index = body.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(batch["results"]):
            raise GameError("角色序号无效")
        return index

    def choose():
        batch = get_batch()
        index = index_for(batch)
        if any(r.get("done") for r in batch["results"][index:]):
            raise GameError("已确认角色及其之前的方案已锁定，请开始新一轮配装再修改")
        prefix = copy.deepcopy(batch["results"][:index+1])
        entry = prefix[-1]
        pick = body.get("pick")
        if isinstance(pick, bool) or not isinstance(pick, int) or not 0 <= pick < len(entry.get("results", [])):
            raise GameError("配装方案序号无效")
        entry["pick"] = pick
        batch["results"] = calculate(batch, prefix)
        return public(batch)

    def equip():
        batch = get_batch()
        index = index_for(batch)
        result = batch["results"][index]
        if not result.get("results"): raise GameError("该角色没有可穿戴的方案")
        if result.get("done"): return public(batch)
        if any(r.get("results") and not r.get("done") for r in batch["results"][:index]):
            raise GameError("请按照角色顺序确认穿戴")
        _validate_results(batch["results"])
        ids = list(result["results"][result["pick"]]["equipIds"].values())
        # 只有服务器保存的候选可以穿戴；再次检查已确认角色的装备归属。
        for previous in batch["results"][:index]:
            if not previous.get("done"): continue
            expected = set(previous["results"][previous["pick"]]["equipIds"].values())
            actual = {plans._oid(e.get("_id")) for e in plans._equips(client) if plans._oid(e.get("EquipRole")) == previous["roleId"]}
            if not expected.issubset(actual): raise GameError("前面角色的装备已变化，请同步后重新配装")
        batch["uncertain"] = True  # 断网或不完整响应后禁止重复发送同一任务。
        plans.equip_to_role(client, result["roleId"], ids, verify=True)
        actual, _ = plans.worn_items(plans._equips(client), result["roleId"])
        if {plans._oid(e.get("_id")) for e in actual} != set(ids):
            raise GameError("服务器尚未确认整套穿戴，请同步账号后查看，勿重复提交")
        result["done"] = True
        batch["fingerprint"] = _fingerprint(client)
        batch["uncertain"] = False
        return {**public(batch), "overview": overview()}

    def save_multi_plan():
        batch = get_batch()
        index = index_for(batch)
        result = batch["results"][index]
        slot = body.get("planIndex")
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < plans.PLAN_COUNT:
            raise GameError("方案序号必须是 1～4")
        if not result.get("results"): raise GameError("该角色没有可保存的方案")
        if not result.get("done") and any(r.get("results") and not r.get("done") for r in batch["results"][:index]):
            raise GameError("请按照角色顺序保存并穿戴")
        _validate_results(batch["results"])
        ids = list(result["results"][result["pick"]]["equipIds"].values())
        saved = plans.save_plan(client, result["roleId"], slot, ids)
        if not saved.get("verified"):
            return {**public(batch), "saved": saved}
        try:
            response = equip()
        except GameError as error:
            raise GameError(f"方案 {slot + 1} 已保存，但穿上未完成：{error}") from None
        return {**response, "saved": saved}

    def reload():
        client.bootstrap()
        acc.equipment_artifact_uncertain = False
        acc.equipment_batch = None
        if hasattr(acc, "note_state_refreshed"): acc.note_state_refreshed()
        return overview()

    def equip_saved_plan():
        role_id, index = body.get("roleId"), body.get("planIndex")
        if not isinstance(role_id, str) or not any(plans._oid(r.get("_id")) == role_id for r in plans._roles(client)):
            raise GameError("请选择有效的配装角色")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < plans.PLAN_COUNT:
            raise GameError("方案序号必须是 1～4")
        saved = plans.query_plans(client, role_id)
        requested = saved["plans"][index]
        if len(requested) > len(plans.SLOTS) or len(requested) != len(set(requested)):
            raise GameError("预存方案包含重复装备或数量异常，请先在游戏内修正")
        ids = plans._slot_ordered(client, requested)
        if not ids:
            raise GameError(f"该角色的方案 {index + 1} 尚未保存装备")
        # 全部 ID 验证完成后才修改穿戴；空缺部位也与预存方案保持一致。
        acc.equipment_batch = None
        error = None
        planned_slots = {plans.equip_slot(e) for e in plans._equips(client)
                         if plans._oid(e.get("_id")) in ids}
        try:
            for equip in list(plans._equips(client)):
                eid = plans._oid(equip.get("_id"))
                if (plans._oid(equip.get("EquipRole")) == role_id and eid not in ids
                        and plans.equip_slot(equip) not in planned_slots):
                    plans.take_off(client, eid, verify=True)
            plans.equip_to_role(client, role_id, ids, verify=True)
        except GameError as exc:
            error = str(exc)
        finally:
            if hasattr(acc, "note_state_refreshed"): acc.note_state_refreshed()
        actual = {plans._oid(e.get("_id")) for e in plans._equips(client)
                  if plans._oid(e.get("EquipRole")) == role_id}
        return {"roleId": role_id, "planIndex": index, "verified": actual == set(ids) and not error,
                "error": error, "overview": overview()}

    def new_round():
        acc.equipment_batch = None
        acc.equipment_excluded_roles = []
        return {"overview": overview()}

    def exclude_role():
        role_id = body.get("roleId")
        if not isinstance(role_id, str) or not any(plans._oid(r.get("_id")) == role_id for r in plans._roles(client)):
            raise GameError("请选择有效的穿戴角色")
        protected = excluded_ids()
        if role_id not in protected:
            protected.append(role_id)
        acc.equipment_excluded_roles = protected
        # 排除一经保存即作废全部旧结果；随后按当前配置从新装备池重新计算。
        acc.equipment_batch = None
        return {"excludedRoles": excluded_roles(), "overview": overview()}

    def take_off_all():
        # 卸装可能部分完成，开始执行即使旧配装批次失效。
        acc.equipment_batch = None
        try:
            result = plans.take_off_all(client)
        finally:
            if hasattr(acc, "note_state_refreshed"): acc.note_state_refreshed()
        return {**result, "overview": overview()}

    def take_off_role():
        role_id = body.get("roleId")
        if not isinstance(role_id, str) or not any(plans._oid(r.get("_id")) == role_id for r in plans._roles(client)):
            raise GameError("请选择有效的当前角色")
        acc.equipment_batch = None
        try:
            result = plans.take_off_role(client, role_id)
        finally:
            if hasattr(acc, "note_state_refreshed"): acc.note_state_refreshed()
        return {**result, "overview": overview()}

    def set_artifact():
        if getattr(acc, "equipment_artifact_uncertain", False):
            raise GameError("羁绊状态尚未同步，请重新进入装备页同步后再更换")
        acc.equipment_batch = None
        try:
            result = bonds.set_equipped(client, body.get("roleId"), body.get("artifactId"))
        except GameError as error:
            result = {"verified": False, "error": str(error)}
        finally:
            if hasattr(acc, "note_state_refreshed"): acc.note_state_refreshed()
        if result.get("uncertain"):
            acc.equipment_artifact_uncertain = True
        elif result.get("changed"):
            acc.equipment_artifact_uncertain = False
        return {**result, "overview": overview()}

    def change_lock():
        result=plans.set_locked(client,body.get('equipId'),body.get('locked',True),all_max=action=='lockAllMax')
        if result.get('changed'):
            acc.equipment_batch=None
            acc.note_inventory_changed()
        return {**result,'overview':overview()}

    actions = {"overview": overview, "reload": reload, "newRound": new_round, "autoMulti": build_multi,
               'setLock':change_lock,'lockAllMax':change_lock,
               "chooseMulti": choose, "equipMulti": equip, "saveMultiPlan": save_multi_plan,
               "takeOffAll": take_off_all, "takeOffRole": take_off_role, "excludeRole": exclude_role,
               "equipSavedPlan": equip_saved_plan, "setArtifact": set_artifact}
    callback = actions.get(str(action or ""))
    if callback is None: raise GameError("无效的装备操作")
    with acc.lock:
        try:
            return callback()
        except auto.BuildError as error:
            raise GameError(str(error)) from None
