"""Entering the native war screen settles its previous battle's reward.

Evidence: docs/guild-capture-20261015.md, cdp-45. WarEndReward.Drop
and the top-level Drop are the SAME receipt and must never be added together.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import math
import time

from .game_client import GameError
from .guild import has_guild, _response_error

TASK_ID='guild_war_reward'
ROUTE='GuildWarHandler.QueryGuildWarDataForSync'
BEIJING=timezone(timedelta(hours=8))


def period(now_ms):
    # A game day starts at 08:00 Beijing, not midnight. Sat/Sun share
    # Friday's war window; a successful Saturday check also completes Sunday.
    day = datetime.fromtimestamp(now_ms / 1000, BEIJING) - timedelta(hours=8)
    offset = {1: 1, 3: 1, 5: 1, 6: 2}.get(day.weekday())
    return (day - timedelta(days=offset)).strftime('%Y-%m-%d') if offset is not None else None


def check_at(war_day, guild):
    # A stable guild/period offset avoids a reset-time stampede and does not
    # move each time the scheduler or UI reads the deadline.
    seed = hashlib.sha256(f'{guild}:{war_day}'.encode()).digest()
    seconds = 60 + int.from_bytes(seed[:2], 'big') % 61
    date = datetime.fromisoformat(war_day).replace(tzinfo=BEIJING, hour=8) + timedelta(days=1, seconds=seconds)
    return int(date.timestamp() * 1000)


def next_window(now_ms, guild=None):
    local = datetime.fromtimestamp(now_ms / 1000, BEIJING)
    for offset in range(8):
        date = (local + timedelta(days=offset)).replace(hour=8, minute=0, second=0, microsecond=0)
        war_day = (date - timedelta(days=1)).strftime('%Y-%m-%d')
        deadline = check_at(war_day, guild)
        if date.weekday() in (1, 3, 5) and deadline > now_ms:
            return deadline


def status(state,params=None,now_ms=None):
    params=params if isinstance(params,dict) else {}
    now_ms=int(time.time()*1000) if now_ms is None else int(now_ms)
    receipt=params.get('lastReward')or{}
    # A login or an explicit enable queues one immediate settlement query.  It
    # is intentionally independent of the Tue/Thu/Sat display schedule: the
    # server may still expose the previous battle receipt on any day.
    if params.get('_forceCheck'):
        if not has_guild(state):
            return {'nextMs':None,'ready':False,'blocked':'未加入佣兵团','lastReward':receipt}
        return {'nextMs':now_ms,'ready':True,'lastReward':receipt,
                'note':'登录/开启后立即检查上次团战奖励'}
    if not has_guild(state):
        return {'nextMs':None,'ready':False,'blocked':'未加入佣兵团','lastReward':receipt}
    current = period(now_ms)
    guild = (state.get('PlayerGuildInfo') or {}).get('GID')
    if isinstance(guild, dict):guild = guild.get('$oid')
    key = f'{guild}:{current}' if current else None
    checked = params.get('checkedPeriods') or {}
    # Migrate the old successful period markers, but do not use the previous
    # hourly nextCheckAtMs as a new daily deadline.
    claimed = params.get('claimedPeriods') or {}
    done = key in checked or (current is not None and current in claimed)
    due = check_at(current, guild) if current else None
    ready = bool(current and not done and now_ms >= due)
    # Login and toggle-enable already wake the scheduler. An unchecked
    # current period is immediately due once its delayed opening has passed.
    deadline = max(now_ms, due) if current and not done else next_window(now_ms, guild)
    note=''
    if receipt:
        stamp=datetime.fromtimestamp(receipt['claimedAtMs']/1000,BEIJING).strftime('%Y-%m-%d %H:%M')
        note=f"上次团战黄票 ×{receipt['yellowTickets']} · 领取时间 {stamp}（北京时间）"
    return {'nextMs':deadline,'ready':ready,'lastReward':receipt,'note':note,'period':current,'periodKey':key}


def _find(value,key,depth=0):
    if depth>8:return None
    if isinstance(value,dict):
        if isinstance(value.get(key),dict):return value[key]
        children=value.values()
    elif isinstance(value,list):children=value
    else:return None
    for child in children:
        result=_find(child,key,depth+1)
        if result is not None:return result
    return None


def run(client,params=None):
    params=params if isinstance(params,dict) else {}
    now_ms=int(time.time()*1000)
    force=bool(params.get('_forceCheck'))
    current=status(client.account_state or {},params,now_ms)
    if current.get('blocked') or (not force and not current['ready']):
        if force and current.get('blocked'):
            # The one-time login/enable wake-up is consumed even when the
            # account is no longer in a guild, so it cannot linger in saved
            # task parameters and mask the normal calendar state later.
            params.pop('_forceCheck', None)
        return {'ok':True,'skipped':True,'detail':current.get('blocked')or'未到团战奖励检查时间'}
    response=client.call(ROUTE,client._auth_data())
    if not isinstance(response,dict) or _response_error(response):
        raise GameError(_response_error(response) if isinstance(response,dict) else '团战奖励响应格式异常')
    war=_find(response,'GuildWarData')
    if war is not None:client.account_state['GuildWarData']=deepcopy(war)
    def mark_checked():
        period_key=current.get('periodKey')
        checked = dict(params.get('checkedPeriods') or {})
        if period_key:
            checked[period_key] = now_ms
        params.update(checkedAtMs=now_ms, checkedPeriods=dict(sorted(checked.items(), key=lambda r: r[1])[-24:]))
        params.pop('nextCheckAtMs', None)
        params.pop('_forceCheck', None)
    reward=_find(response,'WarEndReward')
    drop=reward.get('Drop') if isinstance(reward,dict) else None
    if not isinstance(drop,dict) and isinstance(reward,dict) and reward:
        drop=response.get('Drop')
    entries=drop.get('Items') if isinstance(drop,dict) else None
    if not isinstance(entries,list) or not entries:
        mark_checked()
        return {'ok':True,'skipped':True,'detail':'本次没有可领取的上次团战奖励'}
    war_id=reward.get('WarID')
    war_id=str(war_id).strip() if type(war_id) in (str,int) else ''
    guild_id=(client.account_state.get('PlayerGuildInfo')or{}).get('GID')
    if isinstance(guild_id,dict):guild_id=guild_id.get('$oid')
    receipt_key=f'{guild_id}:{war_id}' if war_id else None
    claimed=params.get('claimedWars')or{}
    if receipt_key and receipt_key in claimed:
        mark_checked()
        return {'ok':True,'skipped':True,'detail':'上次团战奖励已记录'}
    count=0
    for entry in entries:
        item=entry.get('Item') if isinstance(entry,dict) else None
        if not isinstance(item,dict):continue
        if str(item.get('StaticID'))!='6':continue
        number=item.get('Count')
        if type(number) is not int or number<0:raise GameError('团战黄票数量格式异常')
        count+=number
    if count <= 0:
        mark_checked()
        return {'ok':True,'skipped':True,'detail':'本次没有可提示的团战黄票奖励'}
    stamp=int(time.time()*1000)
    try:
        server=float(response['Utc'])
        if math.isfinite(server) and server>0:stamp=int(server*1000 if server<100_000_000_000 else server)
    except (KeyError,ValueError,TypeError,OverflowError):pass
    receipt={'yellowTickets':count,'claimedAtMs':stamp}
    if war_id:receipt['warId']=war_id
    params['lastReward']=receipt
    mark_checked()
    if receipt_key:
        claimed={**claimed,receipt_key:receipt}
        params['claimedWars']=dict(sorted(claimed.items(),key=lambda row:row[1]['claimedAtMs'])[-16:])
    clock=datetime.fromtimestamp(stamp/1000,BEIJING).strftime('%Y-%m-%d %H:%M:%S')
    return {'ok':True,'yellowTickets':count,'claimedAtMs':stamp,'warId':war_id or None,
            'detail':f'已领取上次团战黄票 {count} 张，领取时间 {clock}（北京时间）'}
