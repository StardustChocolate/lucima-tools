"""角色换装与 4 套装备方案。

协议来源：游戏 WebGL 客户端 V113552 的 il2cpp 元数据（global-metadata.dat）。
  EquipPlanHandler.QueryPlanByRoleID  EquipPlanRequest {RoleID}
  EquipPlanHandler.SavePlan           EquipPlanRequest {RoleID, Index, EquipIDs}
  EquipmentHandler.EquipsToRole       EquipsRequest    {RoleID, EquipIDs}
  EquipmentHandler.EquipToRole        EquipRequest     {RoleID, EquipID, UpdateNewIDs}
  EquipmentHandler.TakeOffEquip       EquipRequest     {RoleID, EquipID, UpdateNewIDs}
方案数据 EquipPlanData {_id(=RoleID), PlanEquipIDs, AID}，不在登录响应里，需按角色单独查询；
角色从未存过方案时查询只回 {"Utc"}。
游戏里"切换方案"只是客户端选中某套，真正生效靠 EquipsToRole 把该套装备穿上。
实测（2026-10-20）：PlanEquipIDs 是以 str(Index) 为键的字典，如 {"0":[...],"1":[...]}；
服务器不校验 Index 范围（0～4 都会写入），哪个键对应游戏里的方案 1 由 PLAN_INDEX_BASE 决定。
SavePlan 响应直接带回整份 EquipPlanData；EquipsToRole 回 NewEquips + OldInRoleEquipList，
TakeOffEquip 回 NewEquip，穿别人身上的装备时服务器会自动从原角色身上卸下。
EquipsToRole 的列表里不能含该角色已穿着的装备，否则整单被拒（明文 "_id Same"）。
"""
from __future__ import annotations

from typing import Any

from backend.game_client import GameClient, GameError
from backend.tasks import _oid, item_name, parse_equipment, _equip_ref

from . import stats

PLAN_COUNT = 4
SLOTS = ("weapon", "helmet", "armor", "necklace", "ring", "boots")
# StaticID 末位 -> 部位（同 tasks._BATTLE_SLOT_KEY 的抓包实测：武1头2铠3项4戒5鞋6）
_SLOT_BY_DIGIT = dict(zip("123456", SLOTS))
# 方案 Index 的起点。没有抓包样本时按 C# List 下标取 0，save_plan 会回读核对。
PLAN_INDEX_BASE = 0


def _equips(c: GameClient) -> list[dict]:
    return ((c.account_state or {}).get("EquipmentContainer") or {}).get("Datas") or []


def _roles(c: GameClient) -> list[dict]:
    return ((c.account_state or {}).get("RoleDataContainer") or {}).get("Roles") or []


# 只有一个部位能出的主属性（暴击/暴伤只在项链，命中/抗性只在戒指，速度只在鞋子）
_SLOT_BY_MAIN = {"CriticalRate": "necklace", "CriticalDamageRate": "necklace",
                 "EffectHitRate": "ring", "ResistanceRate": "ring", "SpeedValue": "boots"}
# 当前静态表止于 E0461。E0462～E0468 是后续 78 级活动装备：
# 固定攻击主属性为武器，固定生命主属性为头部（同系列既有装备规则）。
# E0464 春湖玉露璫另由 2026-10-22 游戏头部截图和完整六件面板交叉确认。
# 放在生成表之外，更新静态表时优先采用新的 Part，不覆盖权威资料。
_SLOT_SUPPLEMENT = {
    "E0462": "weapon", "E0463": "helmet", "E0464": "helmet",
    "E0465": "weapon", "E0466": "weapon", "E0467": "weapon", "E0468": "weapon",
}
# 名称关键字（游戏数据表里查不到的新装备才用，如 RTA2026S2纪念项鍊）
_SLOT_BY_NAME = (
    ("necklace", ("项链", "项鍊", "吊坠", "颈饰")),
    ("ring", ("戒指", "手环", "指环", "手镯", "手链")),
    ("boots", ("鞋", "靴", "护膝", "护腿")),
    ("helmet", ("头盔", "帽", "面罩", "头饰", "发饰", "发绳", "头冠", "王冠")),
    ("armor", ("铠甲", "护甲", "护胸", "胸甲", "外套", "衣", "服")),
)


def equip_slot(equip: dict) -> str | None:
    """装备部位：Equipment 表 → 图鉴 → 已核对的新装备 → 专属主属性 → 名称 → 常规编号。

    StaticID 末位只是早期装备的规律，游戏表里有 53 件不符合；新出的纪念装备（E0506 等）
    甚至不在静态表里，末位猜测会把项链当成鞋子。
    """
    sid = str(equip.get("StaticID") or "")
    slot = ((stats.tables().get("parts") or {}).get(sid)
            or (_equip_ref().get(sid) or {}).get("slot") or _SLOT_SUPPLEMENT.get(sid))
    if slot:
        return slot
    main = str((equip.get("MainProp") or {}).get("PropertyType") or "")
    if main in _SLOT_BY_MAIN:
        return _SLOT_BY_MAIN[main]
    name = item_name(sid)
    for slot, words in _SLOT_BY_NAME:
        if any(w in name for w in words):
            return slot
    # 活动编号从 E0421 起不再按六个部位循环；未知条目不能继续按末位猜。
    if sid.startswith("E04") and sid[1:].isdigit() and int(sid[1:]) >= 421:
        return None
    return _SLOT_BY_DIGIT.get(sid[-1:])


def worn_items(equips: list[dict], role_id: str) -> tuple[list[dict], dict]:
    """角色实际生效的装备：每个部位只算一件。

    服务器允许同一部位有多件装备标记在同一角色身上（例如一次提交里带了两条项链），
    游戏客户端按装备列表顺序取最后一件显示和计算（2026-10-20 安泰西亚实测）。
    返回 (生效装备, {部位: [同部位全部装备]})，后者用于提示数据异常。
    """
    by_slot: dict = {}
    conflicts: dict = {}
    for eq in equips:
        if _oid(eq.get("EquipRole")) != role_id:
            continue
        slot = equip_slot(eq)
        if slot in by_slot:
            conflicts.setdefault(slot, [by_slot[slot]]).append(eq)
        by_slot[slot] = eq
    return list(by_slot.values()), conflicts


def _equip_view(equip: dict) -> dict:
    view = parse_equipment(equip)
    view["id"] = _oid(equip.get("_id"))
    view["slot"] = equip_slot(equip)
    view["enhance"] = equip.get("LV") or view.get("enhance") or 0
    view["role"] = _oid(equip.get("EquipRole")) or ""
    view["locked"] = bool(equip.get("IsLock"))
    return view


def _role_view(role: dict) -> dict:
    sid = str(role.get("StaticID") or "")
    return {"id": _oid(role.get("_id")), "staticID": sid, "name": item_name(sid),
            "level": role.get("LV"), "star": role.get("Star")}


def overview(c: GameClient) -> dict:
    """全部角色 + 全部装备（含穿戴者），都来自登录快照，不发请求。"""
    roles = [_role_view(r) for r in _roles(c)]
    names = {r["id"]: r["name"] for r in roles}
    equips = []
    for eq in _equips(c):
        view = _equip_view(eq)
        view["roleName"] = names.get(view["role"], "")
        equips.append(view)
    worn = {}
    for e in equips:
        if e["role"]:
            worn[e["role"]] = worn.get(e["role"], 0) + 1
    for r in roles:
        r["worn"] = worn.get(r["id"], 0)
    roles.sort(key=lambda r: (-(r["worn"] > 0), -(r["star"] or 0), -(r["level"] or 0), r["staticID"]))
    return {"roles": roles, "equips": equips, "planCount": PLAN_COUNT}


def _find_key(value: Any, key: str):
    """在任意嵌套的响应里找第一个出现的 key。"""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _find_key(child, key)
        if found is not None:
            return found
    return None


def _id_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(_oid(v)) for v in value if _oid(v)]


def _plans_from(response: Any) -> list[list[str]]:
    """把 PlanEquipIDs 归一成 4 个装备 ID 列表。

    兼容三种形状：[[...],[...]]、{"0":[...]} / {"1":[...]}（按 PLAN_INDEX_BASE 对齐）、
    以及平铺 24 个 ID（每 6 个一套）。
    """
    raw = _find_key(response, "PlanEquipIDs")
    plans: list[list[str]] = [[] for _ in range(PLAN_COUNT)]
    if isinstance(raw, dict):
        for key, ids in raw.items():
            try:
                idx = int(key) - PLAN_INDEX_BASE
            except (TypeError, ValueError):
                continue
            if 0 <= idx < PLAN_COUNT:
                plans[idx] = _id_list(ids)
    elif isinstance(raw, list) and raw and all(isinstance(x, list) for x in raw):
        for idx, ids in enumerate(raw[:PLAN_COUNT]):
            plans[idx] = _id_list(ids)
    elif isinstance(raw, list):
        flat = _id_list(raw)
        for idx in range(PLAN_COUNT):
            plans[idx] = flat[idx * 6:(idx + 1) * 6]
    return plans


def query_plans(c: GameClient, role_id: str) -> dict:
    if not role_id:
        raise GameError("缺少角色 ID")
    resp = c.call("EquipPlanHandler.QueryPlanByRoleID", c._auth_data({"RoleID": role_id}))
    return {"roleId": role_id, "plans": _plans_from(resp), "raw": resp}


def _slot_ordered(c: GameClient, equip_ids: list[str]) -> list[str]:
    """校验 ID 都在背包里、每个部位最多一件，按 武头铠项戒鞋 排序。"""
    by_id = {_oid(e.get("_id")): e for e in _equips(c)}
    picked: dict[str, str] = {}
    for eid in equip_ids:
        eid = str(eid or "")
        if not eid:
            continue
        equip = by_id.get(eid)
        if equip is None:
            raise GameError(f"装备 {eid} 不在背包中，请刷新账号数据后再试")
        slot = equip_slot(equip)
        if slot in picked and picked[slot] != eid:
            raise GameError(f"同一部位只能放一件装备（{slot}）")
        picked[slot] = eid
    return [picked[s] for s in SLOTS if s in picked]


def save_plan(c: GameClient, role_id: str, index: int, equip_ids: list[str]) -> dict:
    """把装备写进第 index(0~3) 套方案，然后回读核对写到了哪一套。"""
    if not role_id:
        raise GameError("缺少角色 ID")
    if not isinstance(index, int) or not 0 <= index < PLAN_COUNT:
        raise GameError("方案序号必须是 1～4")
    ids = _slot_ordered(c, equip_ids)
    resp = c.call("EquipPlanHandler.SavePlan", c._auth_data({
        "RoleID": role_id, "Index": index + PLAN_INDEX_BASE, "EquipIDs": ids,
    }))
    after = query_plans(c, role_id)
    landed = [i for i, plan in enumerate(after["plans"]) if sorted(plan) == sorted(ids)]
    return {"roleId": role_id, "index": index, "equipIds": ids, "raw": resp,
            "plans": after["plans"], "verified": index in landed, "landedAt": landed}


def _merge_equips(c: GameClient, resp: Any) -> int:
    """把响应里带回的装备对象按 _id 覆盖进快照，返回覆盖件数。"""
    returned = []
    for key in ("NewEquips", "OldInRoleEquipList", "Equips"):
        value = _find_key(resp, key)
        if isinstance(value, list):
            returned.extend(v for v in value if isinstance(v, dict))
    for key in ("NewEquip", "OldInRoleEquip"):
        value = _find_key(resp, key)
        if isinstance(value, dict):
            returned.append(value)
    datas = _equips(c)
    index = {_oid(e.get("_id")): i for i, e in enumerate(datas)}
    merged = 0
    for eq in returned:
        i = index.get(_oid(eq.get("_id")))
        if i is not None:
            datas[i] = eq
            merged += 1
    return merged


def _assume_worn(c: GameClient, role_id: str, ids: list[str]) -> None:
    """响应没带装备对象时，按游戏规则本地推算：新装备归该角色，同部位旧装备卸下。"""
    wanted = set(ids)
    slots = {equip_slot(e) for e in _equips(c) if _oid(e.get("_id")) in wanted}
    for eq in _equips(c):
        eid = _oid(eq.get("_id"))
        if eid in wanted:
            eq["EquipRole"] = role_id
        elif _oid(eq.get("EquipRole")) == role_id and equip_slot(eq) in slots:
            eq["EquipRole"] = ""


def set_locked(c: GameClient, equip_id=None, locked=True, *, all_max=False) -> dict:
    """SwitchLock toggles: refresh before selecting, then verify without replay."""
    if type(locked) is not bool:raise GameError('锁定状态必须是布尔值')
    c.bootstrap()
    rows=_equips(c)
    if all_max:
        ids=[_oid(row['_id']) for row in rows if row.get('LV')==15 and row.get('IsLock') is False]
    else:
        row=next((row for row in rows if _oid(row.get('_id'))==equip_id),None)
        if row is None:raise GameError('装备不存在，请刷新清单')
        if type(row.get('IsLock')) is not bool:raise GameError('装备锁定状态未确认，请刷新清单')
        ids=[] if row['IsLock']==locked else [equip_id]
    errors=[];attempted=[]
    for start in range(0,len(ids),35):
        batch=ids[start:start+35];attempted.extend(batch)
        try:
            response=c.call('EquipmentHandler.SwitchLock',c._auth_data({'EquipIDs':batch}))
            _merge_equips(c,response)
        except Exception as error:
            errors.append(str(error));break
    # A timeout could mean a committed toggle. Never resend a batch blindly.
    try:c.bootstrap()
    except Exception as error:
        return {'verified':False,'changed':bool(attempted),'uncertain':True,'detail':'锁定结果尚未确认，请刷新后查看；'+str(error)}
    actual={_oid(row.get('_id')):row.get('IsLock') for row in _equips(c)}
    confirmed=[eid for eid in attempted if actual.get(eid) is locked]
    verified=all(actual.get(eid) is locked for eid in ids)
    label='已加锁' if locked else '已解锁'
    detail=(f'已加锁 {len(confirmed)} 件 +15 装备' if all_max else label) if verified else f'已确认处理 {len(confirmed)}/{len(ids)} 件，部分锁定状态未确认'
    return {'verified':verified,'changed':bool(attempted),'count':len(confirmed),'locked':locked,'detail':detail,'errors':errors}


def equip_to_role(c: GameClient, role_id: str, equip_ids: list[str], *, verify=False) -> dict:
    """整套穿到角色身上（用于"切换到方案 N"或手动选好装备后一键穿戴）。"""
    if not role_id:
        raise GameError("缺少角色 ID")
    ids = _slot_ordered(c, equip_ids)
    if not ids:
        raise GameError("没有要穿戴的装备")
    # 只发送需要更换的装备：列表里只要有一件已经穿在该角色身上，服务器就整单拒绝并回
    # 明文 "_id Same"（2026-10-20 实测）。游戏客户端也只提交有变化的部位。
    worn = {_oid(e.get("_id")) for e in _equips(c) if _oid(e.get("EquipRole")) == role_id}
    changed = [i for i in ids if i not in worn]
    if not changed:
        return {"roleId": role_id, "equipIds": ids, "changed": [], "raw": None}
    resp = c.call("EquipmentHandler.EquipsToRole", c._auth_data({"RoleID": role_id, "EquipIDs": changed}))
    merged = _merge_equips(c, resp)
    if verify:
        actual, _ = worn_items(_equips(c), role_id)
        if { _oid(e.get("_id")) for e in actual } != set(ids):
            c.bootstrap()
    elif not merged:
        _assume_worn(c, role_id, changed)
    return {"roleId": role_id, "equipIds": ids, "changed": changed, "raw": resp}


def take_off(c: GameClient, equip_id: str, *, verify=False) -> dict:
    equip = next((e for e in _equips(c) if _oid(e.get("_id")) == equip_id), None)
    if equip is None:
        raise GameError("装备不在背包中，请刷新账号数据后再试")
    role_id = _oid(equip.get("EquipRole")) or ""
    if not role_id:
        raise GameError("这件装备没有被穿戴")
    resp = c.call("EquipmentHandler.TakeOffEquip", c._auth_data({
        "RoleID": role_id, "EquipID": equip_id, "UpdateNewIDs": [],
    }))
    merged = _merge_equips(c, resp)
    if verify:
        if any(_oid(e.get("_id")) == equip_id and _oid(e.get("EquipRole")) for e in _equips(c)):
            c.bootstrap()
        if any(_oid(e.get("_id")) == equip_id and _oid(e.get("EquipRole")) for e in _equips(c)):
            raise GameError("服务器尚未确认卸下该装备，请同步账号后查看")
    elif not merged:
        equip["EquipRole"] = ""
    return {"equipId": equip_id, "raw": resp}


def take_off_role(c: GameClient, role_id: str) -> dict:
    """只卸下指定角色的装备，复用逐件确认和最终回读。"""
    if not isinstance(role_id, str) or not role_id:
        raise GameError("缺少角色 ID")
    return _take_off_worn(c, role_id)


def take_off_all(c: GameClient) -> dict:
    return _take_off_worn(c)


def _take_off_worn(c: GameClient, role_id: str | None = None) -> dict:
    """从最新账号快照逐件卸装；单件失败会记录并继续尝试其余装备，最后统一回读确认结果。"""
    c.bootstrap()
    if role_id is not None and not any(_oid(r.get("_id")) == role_id for r in _roles(c)):
        raise GameError("该角色已不在当前账号中，请同步账号后查看")
    worn = {_oid(e.get("_id")): _oid(e.get("EquipRole")) for e in _equips(c)
            if _oid(e.get("_id")) and _oid(e.get("EquipRole"))
            and (role_id is None or _oid(e.get("EquipRole")) == role_id)}
    removed, errors = 0, []
    for equip_id in worn:
        try:
            if role_id is not None and not any(_oid(e.get("_id")) == equip_id and _oid(e.get("EquipRole")) == role_id for e in _equips(c)):
                raise GameError("装备归属已变化，跳过该件并继续卸装")
            take_off(c, equip_id, verify=True)
            removed += 1
        except Exception as exc:
            # One rejected item must never prevent the remaining pieces from
            # being attempted.  Refresh after each failure because the server
            # may have committed the request before returning an error.
            errors.append(f"{equip_id}: {exc}")
            try:
                c.bootstrap()
            except Exception as sync_error:
                errors.append(f"同步失败: {sync_error}")
    try:
        c.bootstrap()
    except Exception as exc:
        detail = "；".join(errors + [f"最终同步失败：{exc}"])
        return {"total": len(worn), "removed": removed, "roleCount": len(set(worn.values())),
                "verified": False, "error": f"{detail}。请同步账号后查看。"}
    remaining = sum(bool(_oid(e.get("EquipRole"))) and (role_id is None or _oid(e.get("EquipRole")) == role_id)
                    for e in _equips(c))
    # 即使请求抛错，也可能已经在服务器生效；以回读数据统计卸下件数。
    still_worn = {_oid(e.get("_id")) for e in _equips(c) if _oid(e.get("EquipRole"))}
    removed = len(set(worn) - still_worn)
    return {"total": len(worn), "removed": removed, "roleCount": len(set(worn.values())),
            "remaining": remaining, "verified": remaining == 0,
            "error": "；".join(errors) or ("仍有装备穿戴在角色身上，请同步后重试。" if remaining else "")}
