"""Monthly VIP supply pack, only during the game's advertised event boosts."""
from . import automation_supplies as rules
from .game_data import activity_open
from .game_client import GameError

TASK_ID='vip_supplies'


def event_state(state,now_ms):
    from .account_buffs import _stamp
    meta=rules.catalog()['vip'];active=[];future=[]
    for row in (state.get('StatusContainer')or{}).get('List')or[]:
        if row.get('StaticID') not in meta['buffs']:continue
        start,end=_stamp(row.get('StartTime')),_stamp(row.get('EndTime'))
        if start is not None and start>now_ms:future.append(start);continue
        if end is not None and end>now_ms:active.append(row['StaticID'])
    # Global event boosts are derived by the native client from Activity.StatusID;
    # they need not appear as individually purchased AccountStatusList entries.
    settings=(state.get('ActivityDBSettingContainer')or{}).get('SettingList')or[]
    for identifier in meta['activities']:
        if activity_open(identifier,state,now=now_ms/1000):active.append(identifier)
        for row in settings:
            if row.get('ActivityID')==identifier:
                start=_stamp(row.get('StartTime'))
                if start is not None and start>now_ms:future.append(start)
    return list(dict.fromkeys(active)),min(future,default=now_ms+600_000)


def status(state,runtime=None,now_ms=None):
    now_ms=rules.now() if now_ms is None else now_ms;runtime=runtime or {};meta=rules.catalog()['vip']
    if runtime.get('uncertain'):
        due=runtime.get('retryAtMs',now_ms)
        return {'ready':now_ms>=due,'nextMs':due,'note':'等待刷新确认上次礼包领取结果'}
    try:
        bought=rules.count(rules.record(state,meta['id'],meta['store']),now_ms,'month')
        if bought>=meta['limit']:return {'ready':False,'nextMs':rules.next_reset(now_ms,'month'),'note':'本月VIP资源补给礼包已领取'}
        from .activity_progress import cleared
        scene=next((row for row in (state.get('SceneDataContainer')or{}).get('Scenes')or[] if row.get('StaticID')==meta['scene']),{})
        if not cleared(scene):return {'ready':False,'nextMs':now_ms+600_000,'note':'通过主线1-3-6后可领取VIP资源补给礼包'}
        active,next_ms=event_state(state,now_ms)
        if not active:return {'ready':False,'nextMs':next_ms,'note':'等待元素／贡献／讨伐／金币活动加成生效'}
        if rules.money(state)<meta['price']:return {'ready':False,'nextMs':now_ms+600_000,'note':'等待1万金币领取VIP资源补给礼包'}
        return {'ready':True,'nextMs':now_ms,'note':'活动加成生效，可领取本月礼包'}
    except GameError as error:return {'ready':False,'nextMs':now_ms+600_000,'note':str(error)}


def run(client,params=None,runtime=None):
    if rules.borrower_busy(client):return {'ok':True,'skipped':True,'detail':'等待当前借装战斗结束后检查礼包'}
    runtime=runtime if runtime is not None else {};now_ms=rules.game_now(client)
    if not status(client.account_state or {},runtime,now_ms)['ready']:return {'ok':True,'skipped':True,'detail':status(client.account_state or {},runtime,now_ms)['note']}
    client.bootstrap();runtime.pop('uncertain',None)
    now_ms=rules.game_now(client);meta=rules.catalog()['vip']
    if not status(client.account_state,runtime,now_ms)['ready']:return {'ok':True,'skipped':True,'detail':status(client.account_state,runtime,now_ms)['note']}
    from .tasks import _buy_payload
    from .equipment_sale import reward_summary
    current=rules.record(client.account_state,meta['id'],meta['store'])
    try:
        response=client.call('StoreHandler.BuyCommodity',_buy_payload(client,current,1))
        rec=response.get('CommodityRecord')or{}
        if rec.get('StaticID')!=meta['id'] or rec.get('Store')!=meta['store'] or rec.get('BuyCount')!=1:
            raise GameError('VIP资源补给礼包领取响应未确认')
        client._apply_commodity_record(response);client._apply_now_items(response)
        rewards=reward_summary(response)
        gain='、'.join(f"{row['name']} ×{row['count']}" for row in rewards)
        detail='活动加成期间已领取本月VIP资源补给礼包（花费1万金币）'
        detail+='，获得 '+gain if gain else '，奖励请查看背包或邮件'
        return {'ok':True,'detail':detail,'rewardSummary':rewards}
    except Exception as error:
        runtime.update(uncertain=True,retryAtMs=rules.now()+300_000)
        return {'ok':False,'retryable':True,'detail':f'礼包领取结果未确认：{error}；5分钟后刷新确认，不重复发送旧请求'}
