"""羁绊目录、常驻属性与穿戴确认。

协议：ArtifactHandler.Equip / UnEquip {RoleID, ArtifactID}。
响应：NewArtifact / OldInRoleArtifact / Artifact；归属不完整时重新读取账号。
常驻属性复用已验证的 Artifact 等级插值和 AddValue 合并，不把触发效果当成面板。
"""
from __future__ import annotations

import copy

from backend.abyss_state import metadata
from backend.abyss_properties import artifact_properties, combine_functions, float32
from backend.game_client import GameError
from backend.tasks import _oid, item_name, _localization, _abyss_skill_text

JOBS = {"Warrior": "战士", "Caster": "术师", "Sniper": "狙击", "Vanguard": "先锋", "Defender": "重装", "Medic": "医疗", "Other": "通用"}
PANEL_MODIFIERS = {"HPValue", "AttackValue", "DefenceValue", "SpeedValue", "HPRate", "AttackRate", "DefenceRate", "SpeedRate",
                   "HPFinalRate", "AttackFinalRate", "DefenceFinalRate", "SpeedFinalRate", "CriticalRate", "CriticalDamageRate", "EffectHitRate", "ResistanceRate", "PinchRate"}
LABELS = {"HPValue": "生命", "AttackValue": "攻击", "DefenceValue": "防御", "SpeedValue": "速度", "HPRate": "生命", "AttackRate": "攻击", "DefenceRate": "防御", "SpeedRate": "速度",
          "HPFinalRate": "最终生命", "AttackFinalRate": "最终攻击", "DefenceFinalRate": "最终防御", "SpeedFinalRate": "最终速度", "CriticalRate": "暴击率", "CriticalDamageRate": "暴击伤害", "EffectHitRate": "状态命中", "ResistanceRate": "状态抗性", "PinchRate": "追击率"}


def inventory(state):
    return ((state or {}).get("Artifacts") or {}).get("Datas") or []


def worn(state, role_id):
    return [art for art in inventory(state) if _oid(art.get("EquipRole")) == role_id]


def job(role):
    return metadata()["tables"]["Role"].get(str(role.get("StaticID") or ""), {}).get("Job", "")


def permanent_mods(artifact):
    """Return full base values plus unconditional self properties, at actual LV."""
    table = metadata()["tables"]
    if artifact.get("StaticID") not in table["Artifact"]:
        return {}, True
    level = max(1, min(30, int(artifact.get("LV") or 1)))
    props = artifact_properties({**artifact, "LV": level}, table)
    result = {key: value for key, value in props.values.items() if key in PANEL_MODIFIERS}
    definition = table["Artifact"][artifact["StaticID"]]
    for key in ("HPValue", "AttackValue"):
        if key in result:
            initial = float(definition.get("Base." + key) or 0)
            maximum = float(definition.get("Max." + key) or 0)
            native_base = initial + (maximum - initial) * float32(float32(level - 1) / float32(29))
            # 延续已核对截图的基础插值取整，保留 Dynamic/LVProp 的额外固定值。
            result[key] = int(initial + (maximum - initial) * ((level - 1) / 29)) + (result[key] - native_base)
    omitted = False
    for fun in combine_functions(props.functions):
        prop = fun.get("Property")
        if (fun.get("Trigger") == "Property" and prop in PANEL_MODIFIERS and not fun.get("IsAura")
                and not fun.get("NoSelf") and not fun.get("Condition") and not fun.get("Conditions") and not fun.get("PreCond")
                and not fun.get("ValueConvert") and fun.get("Target", "Self") in ("", "Self")
                and fun.get("TargetCamp", "My") in ("", "My", "Self")):
            result[prop] = result.get(prop, 0) + float(fun.get("Value") or 0)
        else:
            omitted = True
    return result, omitted


def modifier_labels(mods):
    result = []
    for key, value in mods.items():
        if not value or key not in LABELS:
            continue
        percent = key.endswith("Rate")
        number = round(value * (100 if percent else 1), 1)
        result.append(f"{LABELS[key]} {number:+g}{'%' if percent else ''}")
    return result


def catalog(state):
    table = metadata()["tables"]["Artifact"]
    names = {_oid(role.get("_id")): item_name(role.get("StaticID"))
             for role in ((state or {}).get("RoleDataContainer") or {}).get("Roles") or []}
    output = []
    for artifact in inventory(state):
        sid, aid = str(artifact.get("StaticID") or ""), _oid(artifact.get("_id"))
        if not aid:
            continue
        definition = table.get(sid, {})
        mods, omitted = permanent_mods(artifact)
        owner = _oid(artifact.get("EquipRole")) or ""
        output.append({"id": aid, "staticID": sid, "name": item_name(sid), "level": int(artifact.get("LV") or 1),
                       "star": int(definition.get("Star") or 0), "job": definition.get("Job", ""),
                       "jobName": JOBS.get(definition.get("Job"), "职业资料暂缺"), "role": owner,
                       "roleName": names.get(owner, owner), "bonuses": modifier_labels(mods),
                       "effect": _abyss_skill_text(_localization().get(f"T_Artifact_FunDescription_{sid}")) or "效果说明暂缺",
                       "omittedEffects": omitted, "metadataMissing": not bool(definition)})
    return output


def _merge(client, response):
    if not isinstance(response, dict):
        return
    by_id = {_oid(art.get("_id")): art for art in inventory(client.account_state)}
    for key in ("NewArtifact", "OldInRoleArtifact", "Artifact"):
        values = response.get(key)
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, dict) and _oid(value.get("_id")) in by_id:
                by_id[_oid(value["_id"])].update(copy.deepcopy(value))


def set_equipped(client, role_id, artifact_id):
    state = client.account_state or {}
    role = next((role for role in (state.get("RoleDataContainer") or {}).get("Roles") or [] if _oid(role.get("_id")) == role_id), None)
    if not isinstance(role_id, str) or role is None:
        raise GameError("请选择有效的角色")
    if not isinstance(artifact_id, str):
        raise GameError("羁绊编号无效")
    current = worn(state, role_id)
    if len(current) > 1:
        raise GameError("角色羁绊归属异常，请先同步账号")
    if artifact_id:
        selected = next((art for art in inventory(state) if _oid(art.get("_id")) == artifact_id), None)
        if selected is None:
            raise GameError("羁绊不在当前账号背包中")
        definition = metadata()["tables"]["Artifact"].get(selected.get("StaticID"))
        if not definition:
            raise GameError("缺少该羁绊的属性资料，暂不能替换")
        if definition.get("Job") not in ("Other", job(role)):
            raise GameError("该羁绊不适用于此角色的职业")
        if len(current) == 1 and _oid(current[0].get("_id")) == artifact_id:
            return {"verified": True, "changed": False}
        action, sent_id = "ArtifactHandler.Equip", artifact_id
    elif current:
        action, sent_id = "ArtifactHandler.UnEquip", _oid(current[0].get("_id"))
    else:
        return {"verified": True, "changed": False}
    problem = None
    try:
        response = client.call(action, client._auth_data({"RoleID": role_id, "ArtifactID": sent_id}))
        _merge(client, response)
    except GameError as error:
        # 请求可能已到达服务器；只读回实际状态，不重复提交穿戴。
        problem = str(error)
    expected = {artifact_id} if artifact_id else set()
    actual = {_oid(art.get("_id")) for art in worn(client.account_state, role_id)}
    if problem or actual != expected:
        try:
            client.bootstrap()
        except GameError as error:
            return {"verified": False, "changed": True, "uncertain": True, "error": f"无法确认羁绊状态，请同步账号后重试：{error}"}
        actual = {_oid(art.get("_id")) for art in worn(client.account_state, role_id)}
    return {"verified": actual == expected, "changed": True,
            "error": None if actual == expected else problem or "服务器尚未确认羁绊穿戴，已同步实际状态"}
