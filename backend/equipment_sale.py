"""Sweep equipment sales: fixed eligibility and server-confirmed rewards only.

Protocol: EquipmentHandler.SellEquips, authenticated {IDs: [equipment OIDs]}.
Powders / SellPrice are optional client estimates and are deliberately omitted.
The server's Drop.Items is the source of truth for the proceeds.
"""
from __future__ import annotations

from copy import deepcopy

from .game_client import GameError

BATCH_SIZE = 35
POWDER_IDS = ("EC14", "EC24", "EC34", "EC44", "EC54", "EC64")


def reward_summary(response: dict) -> list[dict]:
    from .tasks import item_name
    totals = {}
    for entry in (response.get("Drop") or {}).get("Items") or []:
        if not isinstance(entry, dict):
            continue
        for kind in ("Item", "Equipment", "Role", "Artifact"):
            value = entry.get(kind)
            if not isinstance(value, dict) or not value.get("StaticID"):
                continue
            sid = str(value["StaticID"])
            count = value.get("Count", 1 if kind != "Item" else 0)
            if isinstance(count, bool) or not isinstance(count, (int, float)) or count <= 0:
                continue
            key = (kind, sid)
            totals[key] = totals.get(key, 0) + count
    return [{"kind": kind, "id": sid, "name": item_name(sid), "count": count}
            for (kind, sid), count in totals.items()]


def merge_equipment_drop(client, response: dict):
    """FinishScene is incremental; retain earlier drops until sold or equipped."""
    from .tasks import _oid
    state = client.account_state
    if not isinstance(state, dict):
        return
    rows = state.setdefault("EquipmentContainer", {}).setdefault("Datas", [])
    index = {_oid(row.get("_id")): row for row in rows}
    for entry in (response.get("Drop") or {}).get("Items") or []:
        equip = entry.get("Equipment") if isinstance(entry, dict) else None
        eid = _oid(equip.get("_id")) if isinstance(equip, dict) else None
        if not eid:
            continue
        if eid in index:
            index[eid].update(deepcopy(equip))
        else:
            item = deepcopy(equip)
            rows.append(item)
            index[eid] = item


def eligible(equip: dict, references: dict, *, include_epic85=False) -> bool:
    from .tasks import _oid
    if not isinstance(equip, dict) or not _oid(equip.get("_id")):
        return False
    if equip.get("IsLock") not in (None, False, 0, "0", "false", "False"):
        return False
    if _oid(equip.get("EquipRole")):
        return False
    # Native EquipmentSaveData.LV is the enhancement level (new drops use 0).
    # The item level, e.g. 51 or 85, comes from the StaticID reference below.
    # Keep legacy aliases, and retain the item if any supplied value disagrees.
    enhancement = [equip[key] for key in ('LV', 'Enhance', 'EnhanceLevel') if key in equip]
    # Missing/malformed upgrade data is not proof of an unenhanced item.
    if not enhancement or any(type(value) is bool or value not in (0, 0.0, '0') for value in enhancement):
        return False
    level = (references.get(equip.get("StaticID")) or {}).get("level")
    tier = equip.get("ClassLV")
    # Unknown levels/tiers are retained rather than guessed from the ID.
    if (type(level) is not int or level <= 0 or type(tier) is not int
            or tier not in range(5)):
        return False
    if tier == 3 and level >= 85:
        return include_epic85 is True and level == 85
    return tier != 4 or level <= 70


def sell(client, *, include_epic85=False) -> dict:
    from .tasks import _equip_ref, _oid, _fmt_items
    if getattr(client,'_auto_chip_crafting_enabled',False):
        from . import chip_crafting
        context=getattr(client,'_chip_crafting_context',lambda:{})()
        try:result=chip_crafting.run(client,runtime=context)
        except Exception as error:
            from .automation_supplies import now
            context.update(uncertain=True,retryAtMs=now()+300_000)
            result={'ok':False,'detail':f'自动强化芯片暂未完成：{error}；已保留70级稀有、史诗装备，5分钟后重试'}
        report=getattr(client,'_chip_crafting_on_result',None)
        if callable(report) and not result.get('skipped'):report(result)
    if getattr(client, "_equipment_sale_uncertain", False):
        # Only an explicit subsequent enable can reach here; refresh before
        # selecting IDs again after a lost reply, never resend the old batch.
        client.bootstrap()
        client._equipment_sale_uncertain = False
    state = client.account_state or {}
    refs = _equip_ref()
    reserved = set(getattr(client, '_intimacy_reserved_equipment', set())) | set(getattr(client, '_automation_reserved_equipment', set()))
    from .abyss_loadout import reserved_equipment
    reserved.update(reserved_equipment(client))
    from .chip_crafting import protected_ids
    reserved.update(protected_ids(client))
    candidates = list(dict.fromkeys(
        _oid(row.get("_id")) for row in (state.get("EquipmentContainer") or {}).get("Datas") or []
        if _oid(row.get('_id')) not in reserved and eligible(row, refs, include_epic85=include_epic85)))
    sold = 0
    rewards = {}
    error = None
    for offset in range(0, len(candidates), BATCH_SIZE):
        # Recheck each batch against current ownership/locks, under Account.lock.
        selected = set(candidates[offset:offset + BATCH_SIZE])
        ids = [_oid(row.get("_id")) for row in state["EquipmentContainer"]["Datas"]
               if _oid(row.get("_id")) in selected and eligible(row, refs, include_epic85=include_epic85)]
        ids = list(dict.fromkeys(ids))
        if not ids:
            continue
        try:
            response = client.call("EquipmentHandler.SellEquips", client._auth_data({"IDs": ids}))
            received = reward_summary(response)
            items = [row for row in received if row["kind"] == "Item"]
            if not items:
                raise GameError("服务器未确认卖装收益，请刷新账号状态核对；未自动重试")
        except Exception as exc:
            client._equipment_sale_uncertain = True
            error = str(exc)
            break
        for row in items:
            key = row["id"]
            rewards[key] = rewards.get(key, 0) + row["count"]
        sold += len(ids)
        removed = set(ids)
        state["EquipmentContainer"]["Datas"] = [
            row for row in state["EquipmentContainer"]["Datas"] if _oid(row.get("_id")) not in removed]
    from .tasks import item_name
    detail = f"已贩卖 {sold} 件装备，获得 {_fmt_items(rewards)}" if sold else "没有符合规则的可贩卖装备"
    if error:
        detail += f"；自动贩卖已暂停：{error}"
    return {"ok": error is None, "sold": sold, "detail": detail,
            "rewardSummary": [{"kind": "Item", "id": sid, "name": item_name(sid), "count": count}
                              for sid, count in rewards.items()]}
