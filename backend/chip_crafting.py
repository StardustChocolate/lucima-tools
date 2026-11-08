"""Weekly chip alchemy, preserving its eligible equipment ahead of sweep sales."""
from copy import deepcopy
import math
import struct
from . import automation_supplies as rules
from .game_client import GameError

TASK_ID='chip_crafting'


def material(row,refs):
    from .tasks import _oid
    return (bool(_oid(row.get('_id'))) and row.get('IsLock') is False and not _oid(row.get('EquipRole'))
            and row.get('LV')==0 and row.get('ClassLV') in (2,3)
            and (refs.get(row.get('StaticID'))or{}).get('level')==70)


def remaining(state,now_ms):
    return [(recipe,max(0,recipe['limit']-rules.count(rules.record(state,recipe['id'],recipe['store']),now_ms,'week')))
            for recipe in rules.catalog()['recipes']]


def score(row,state):
    level=(state.get('ArkScienceParkData')or{}).get('IngredientStorageBuildingLV',0)
    if type(level) is not int or level not in range(4):raise GameError('技术开发材料仓库等级未确认')
    f=lambda value:struct.unpack('<f',struct.pack('<f',value))[0]
    base=int(70*rules.catalog()['equipmentQualityMultipliers'][str(row['ClassLV'])])
    return math.ceil(f(f(base)*f(rules.catalog()['ingredientMultipliers'][level])))


def batch(state,recipe,reserved=()):
    from .tasks import _equip_ref,_oid
    refs=_equip_ref();rows=[row for row in (state.get('EquipmentContainer')or{}).get('Datas')or[]
                          if material(row,refs) and _oid(row['_id']) not in reserved]
    chosen=[];total=0
    for row in sorted(rows,key=lambda row:(-row['ClassLV'],_oid(row['_id']))):
        if len(chosen)>=recipe['maxMaterials'] or total>=recipe['targetScore']:break
        chosen.append(row);total+=score(row,state)
    return chosen if len(chosen)==recipe['maxMaterials'] or total>=recipe['targetScore'] else []


def status(state,runtime=None,now_ms=None):
    now_ms=rules.now() if now_ms is None else now_ms;runtime=runtime or {}
    if runtime.get('uncertain'):
        due=runtime.get('retryAtMs',now_ms)
        return {'ready':now_ms>=due,'nextMs':due,'note':'等待刷新确认上次炼制结果'}
    try:
        pending=[recipe for recipe,left in remaining(state,now_ms) if left]
        if not pending:return {'ready':False,'nextMs':rules.next_reset(now_ms,'week'),'note':'本周两种强化芯片均已炼制完成'}
        ready=any(batch(state,recipe) and rules.money(state)>=recipe['price'] for recipe in pending)
    except GameError as error:return {'ready':False,'nextMs':now_ms+600_000,'note':str(error)}
    return {'ready':ready,'nextMs':now_ms if ready else now_ms+600_000,
            'note':'材料已凑满，可炼制' if ready else '等待满批材料或金币；保留70级稀有、史诗装备'}


def protected_ids(client):
    if not getattr(client,'_auto_chip_crafting_enabled',False):return set()
    from .tasks import _equip_ref,_oid
    try:pending=any(left for _,left in remaining(client.account_state or {},rules.game_now(client)))
    except GameError:pending=True
    refs=_equip_ref()
    return {_oid(row.get('_id')) for row in (client.account_state.get('EquipmentContainer')or{}).get('Datas')or[]
            if pending and row.get('ClassLV') in (2,3) and (refs.get(row.get('StaticID'))or{}).get('level')==70}


def run(client,params=None,runtime=None):
    runtime=runtime if runtime is not None else {}
    if rules.borrower_busy(client):return {'ok':True,'skipped':True,'detail':'等待当前借装战斗结束后炼制，材料继续保留'}
    if not status(client.account_state or {},runtime,rules.game_now(client))['ready']:
        return {'ok':True,'skipped':True,'detail':status(client.account_state or {},runtime,rules.game_now(client))['note']}
    client.bootstrap()
    runtime.pop('uncertain',None)
    from .tasks import _oid,_to_send_format
    from .equipment_sale import reward_summary
    from .abyss_loadout import reserved_equipment
    lines=[];rewards=[]
    for recipe,left in remaining(client.account_state,rules.game_now(client)):
        for _ in range(left):
            reserved=reserved_equipment(client)|set(getattr(client,'_intimacy_reserved_equipment',set()))|set(getattr(client,'_automation_reserved_equipment',set()))
            rows=batch(client.account_state,recipe,reserved)
            if not rows or rules.money(client.account_state)<recipe['price']:break
            current=rules.record(client.account_state,recipe['id'],recipe['store'])
            before=rules.count(current,rules.game_now(client),'week');ids={_oid(row['_id']) for row in rows}
            try:
                response=client.call('StoreHandler.Craft',client._auth_data({'CommodityID':recipe['id'],'RecordID':_oid(current.get('_id'))or'',
                    'CostMaterials':[_to_send_format({'Equipment':deepcopy(row)}) for row in rows]}))
                rec=response.get('CommodityRecord')or{}
                received=reward_summary(response)
                if (rec.get('StaticID')!=recipe['id'] or rec.get('Store')!=recipe['store']
                        or rec.get('BuyCount')!=before+1 or not received):
                    raise GameError('炼制响应未确认次数或奖励')
                client._apply_commodity_record(response)
                client._apply_now_items(response)
                client.account_state['EquipmentContainer']['Datas']=[row for row in client.account_state['EquipmentContainer']['Datas'] if _oid(row.get('_id')) not in ids]
                rewards.extend(received)
                gain='、'.join(f"{item['name']} ×{item['count']}" for item in received)
                lines.append(f"{recipe['name']}：消耗 {len(ids)} 件材料，获得 {gain}")
            except Exception as error:
                # Reconcile before another attempt; the exact material batch is
                # never replayed from a stale snapshot, including before sales.
                runtime.update(uncertain=True,retryAtMs=rules.now()+300_000)
                return {'ok':False,'retryable':True,'detail':'；'.join([*lines,f'炼制结果未确认：{error}，5分钟后刷新再检查']),
                        'lines':lines,'rewardSummary':rewards}
    runtime.pop('uncertain',None)
    return {'ok':True,'skipped':not lines,'detail':'；'.join(lines)or'等待满批材料或金币','lines':lines,'rewardSummary':rewards}
