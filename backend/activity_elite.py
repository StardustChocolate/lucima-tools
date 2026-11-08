"""At most five elite battles, with durable borrowed-asset cleanup per attempt."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
import threading

from . import abyss_loadout, activity_team, config
from .game_client import GameError
from .logutil import log

ATTEMPT_LIMIT = 5


def run(client, sid, *, cancel_event, support_provider=None, selected_support=None,
        account_key=None, account_lock=None, on_progress=None):
    from .activity_progress import validate_scene, start_info, records, cleared, retain_confirmed_clear
    from .abyss_battle import run_activity_battle, BattleUnconfirmed, SupportUnavailable
    account_key = account_key or getattr(client,'login_id',None) or getattr(client,'aid',None)
    if not account_key:
        raise GameError('缺少账号标识，无法保存活动借装归还记录')
    excluded, usage, attempts = set(), Counter(), []
    unavailable_supports = set()
    isolated=abyss_loadout.protected_deferred_assets(client,account_key)
    excluded_equipment, excluded_artifacts = isolated['equipment'], isolated['artifacts']
    recovery = {'restored': True}
    loan_ids = set()
    owner = getattr(client,'_automation_asset_owner',None)
    if owner is not None and owner != threading.get_ident():
        raise GameError('当前账号还有借装任务，请等待归还后再推图')
    old_owner, old_label, old_reserved = owner, getattr(client,'_automation_asset_label',None), getattr(client,'_automation_reserved_equipment',set())
    client._automation_asset_owner = threading.get_ident()
    client._automation_asset_label = '活动自动推图'
    client._automation_reserved_equipment = set()

    def publish(detail, **extra):
        if on_progress:
            on_progress({'detail':detail,'battle':None,**extra})

    def finish(ok, detail, *, confirmed=True):
        return {'ok':ok,'confirmed':confirmed,'detail':detail,'attempts':attempts,
                'recoveryRequired':not recovery.get('restored',False)}

    try:
        if abyss_loadout.pending(client,account_key):
            publish('第13关将自动配队，不使用所选队伍；正在处理上次借装',phase='restoring')
            old = abyss_loadout.recover_before_challenge(client,account_key=account_key)
            if not old.get('restored') and not old.get('closed'):
                recovery=old
                return finish(False,'上次借用装备与羁绊尚未完全归还：'+str(old.get('error')or'等待核对'),confirmed=False)
            old_reserved = set()  # The old journal has been resolved or archived.
            excluded_equipment.update(old.get('unresolvedEquipment') or [])
            excluded_artifacts.update(old.get('unresolvedArtifacts') or [])
        for attempt in range(1,ATTEMPT_LIMIT+1):
            if cancel_event.is_set():
                return finish(False,'自动推图已停止，装备与羁绊已归还')
            state = client.account_state or {}
            if cleared(records(state).get(sid)):
                client._activity_pending=None
                return finish(True,'服务器已确认第13关通关，无需重复挑战')
            scene = validate_scene(sid)
            energy = next((item for item in state.get('ItemContainer',{}).get('Items') or [] if item.get('StaticID')=='3'),None)
            if energy is not None and int(energy.get('Count') or 0)<int(scene.get('Energy') or 0):
                return finish(False,'体力不足，精英关自动挑战已停止，装备与羁绊已归还')
            publish(f'第13关自动配队，不使用所选队伍 · 第 {attempt}/{ATTEMPT_LIMIT} 次尝试',
                    phase='planning',attempt=attempt,attemptLimit=ATTEMPT_LIMIT)
            friends = []
            if support_provider:
                try:
                    friends = list(support_provider() or [])
                except Exception:
                    publish('好友助战暂未刷新，使用已选择的可用助战继续配队',phase='planning')
            if selected_support is not None and not friends:
                friends = [selected_support]
            friends = [entry for entry in friends if str(entry.get('PlayerInfo',{}).get('CUID')) not in unavailable_supports]
            try:
                team = activity_team.choose(state,scene,friends,excluded=excluded,usage=usage,cancelled=cancel_event.is_set)
            except GameError as error:
                return finish(False,str(error))
            excluded.add(team['key'])
            usage.update(team['staticIds'])
            if team['supportStaticId']:
                usage['support:'+team['supportStaticId']]+=1
            entry = {'attempt':attempt,'roles':list(team['roles']),'support':team['supportStaticId'],
                     'team':list(team['names']),'battleAttempted':False}
            attempts.append(entry)
            outcome, failure = None, None
            before_clear = cleared(records(state).get(sid))
            confirmed_record = None
            try:
                publish('正在自动借装：'+ '、'.join(team['names']) + ('；好友替补助战：'+team['supportName'] if team['support'] else ''),
                        phase='equipping',team=team['names'],supportName=team['supportName'])
                plan = abyss_loadout.plan(state,{'upper':team['roles'],'lower':[]},role_ids=team['roles'],
                    cancelled=cancel_event.is_set,progress=lambda value:publish(value.get('message') or '正在配置装备与羁绊',phase='equipping'),
                    offensive=True,equip_artifacts=True,survival_fallback=True,utility_roles=team['utilityRoles'],
                    profile_overrides=team['profileOverrides'],exclude_equipment=excluded_equipment,exclude_artifacts=excluded_artifacts)
                loan_ids = set(plan['equipment'])
                client._automation_reserved_equipment = loan_ids
                abyss_loadout.apply(client,plan,account_key=account_key,cancelled=cancel_event.is_set)
                if cancel_event.is_set():
                    raise GameError('自动推图已停止')
                info = start_info(client.account_state or {},sid,'',team['support'],role_positions=team['positions'])
                publish(f'第13关自动阵容挑战中 · 第 {attempt}/{ATTEMPT_LIMIT} 次',phase='battle')
                entry['battleAttempted'] = True
                # Only the selected local heroes are borrowed. Friend roles and
                # user-saved team settings are never changed.
                if account_lock:
                    account_lock.release()
                def battle_progress(value):
                    if value.get('support'):entry['supportStatus']=dict(value['support'])
                    if on_progress:on_progress({'detail':f'第13关自动阵容挑战中 · 第 {attempt}/{ATTEMPT_LIMIT} 次',
                        'phase':'battle','battle':value})
                try:
                    outcome = run_activity_battle(client,info,cancel_event=cancel_event,
                        use_soul=True,prioritize_soul_break=True,reserve_boss_tools=True,
                        on_progress=battle_progress)
                finally:
                    if account_lock:
                        account_lock.acquire()
                if outcome.get('confirmed'):
                    client._activity_pending = None
            except Exception as error:
                failure = error
                entry['error'] = str(error)
                if not isinstance(error,GameError):
                    log.exception('[activity] elite scene=%s attempt=%s failed',sid,attempt)
            finally:
                settled = records(client.account_state or {}).get(sid)
                if not before_clear and cleared(settled):
                    confirmed_record = deepcopy(settled)
                if abyss_loadout.pending(client,account_key):
                    try:publish('正在归还本次借用的装备与羁绊',phase='restoring')
                    except Exception:pass  # A display callback must never prevent asset returns.
                    try:
                        recovery = abyss_loadout.restore(client,account_key=account_key)
                    except Exception as error:
                        recovery = {'restored':False,'error':str(error)}
                if recovery.get('restored'):
                    client._automation_reserved_equipment = set()
                if confirmed_record:
                    retain_confirmed_clear(client.account_state,confirmed_record)
            if not recovery.get('restored'):
                return finish(False,'装备与羁绊归还尚未确认，已停止自动推图：'+str(recovery.get('error') or '请稍后刷新确认'),confirmed=False)
            if cancel_event.is_set():
                return finish(False,'自动推图已停止，装备与羁绊已归还')
            current_clear = cleared(records(client.account_state or {}).get(sid))
            verified_uncleared = False
            uncertain = (isinstance(failure,BattleUnconfirmed) or failure and not isinstance(failure,GameError)
                         or outcome and (outcome.get('ok') or not outcome.get('confirmed')))
            if entry['battleAttempted'] and not current_clear and uncertain:
                # A post-settlement parsing/display error may close the socket
                # before the next login can see the newly committed record.
                for seconds in (.3,.8):
                    publish('正在同步服务器，核对第13关通关记录',phase='checking')
                    if account_lock:account_lock.release()
                    try:stopped=cancel_event.wait(seconds)
                    finally:
                        if account_lock:account_lock.acquire()
                    if stopped:break
                    try:client.bootstrap()
                    except GameError:continue
                    verified_uncleared=True
                    current_clear=cleared(records(client.account_state or {}).get(sid))
                    if current_clear:break
            if cancel_event.is_set():
                return finish(False,'自动推图已停止，装备与羁绊已归还')
            if current_clear and entry['battleAttempted'] and not before_clear:
                entry['won'] = True
                if failure:entry['recoveredSettlement'] = True
                client._activity_pending = None
                return {**(outcome or {}),**finish(True,'第13关已通关，装备与羁绊已归还')}
            retry_after_sync = verified_uncleared and (
                isinstance(failure,(KeyError,TypeError,ValueError,BattleUnconfirmed))
                or outcome and (not outcome.get('confirmed') or outcome.get('ok')))
            if retry_after_sync:
                client._activity_pending=None
                entry['won']=False;entry['syncedUncleared']=True
                publish(f'服务器核对第13关仍未通关，装备与羁绊已归还，准备更换下一组阵容（{attempt}/{ATTEMPT_LIMIT}）',phase='retrying')
                if team['support'] and entry.get('supportStatus',{}).get('status') in ('reserved','joined'):
                    unavailable_supports.add(str(team['support']['PlayerRoleData']['PlayerInfo'].get('CUID')))
                if attempt<ATTEMPT_LIMIT:
                    if account_lock:account_lock.release()
                    try:cancel_event.wait(.1)
                    finally:
                        if account_lock:account_lock.acquire()
                continue
            if failure:
                if isinstance(failure,SupportUnavailable) and team['support']:
                    cuid = team['support'].get('PlayerRoleData',{}).get('PlayerInfo',{}).get('CUID')
                    unavailable_supports.add(str(cuid))
                    entry['battleAttempted'] = False
                    entry['supportUnavailable'] = True
                    client._activity_pending = None
                    publish('所借助战已使用，装备与羁绊已归还；正在改用其他好友或自有角色',phase='retrying')
                    continue
                if entry['battleAttempted'] or not isinstance(failure,abyss_loadout.LoadoutUnavailable):
                    if entry['battleAttempted']:
                        client._activity_pending = (client.account_state,sid)
                    return finish(False,f'精英关挑战停止：{failure}；装备与羁绊已归还',confirmed=False)
                publish('本阵容无法完成配装，准备改用其他角色',phase='retrying')
                continue
            if not outcome or not outcome.get('confirmed'):
                client._activity_pending = (client.account_state,sid)
                return finish(False,'精英关结果尚未确认，已停止重试；装备与羁绊已归还，请刷新后确认',confirmed=False)
            if outcome.get('ok'):
                client._activity_pending = (client.account_state,sid)
                return finish(False,'精英关通关记录尚未同步，已停止重试；装备与羁绊已归还，请刷新后确认',confirmed=False)
            entry['won'] = False
            if team['support']:
                # A confirmed loss has exhausted the team and its reserve.
                # Do not spend the next attempt on a known used supporter.
                cuid = team['support'].get('PlayerRoleData',{}).get('PlayerInfo',{}).get('CUID')
                unavailable_supports.add(str(cuid))
            publish(f'第 {attempt} 次未通关，装备与羁绊已归还，准备更换阵容',phase='retrying')
            if attempt<ATTEMPT_LIMIT:
                delay=config.get_request_delay_config()
                seconds=max(.05,float(delay.get('ms') or 0)/1000) if delay.get('enabled') else .05
                if account_lock:account_lock.release()
                try:cancel_event.wait(seconds)
                finally:
                    if account_lock:account_lock.acquire()
        battles = sum(row['battleAttempted'] for row in attempts)
        return finish(False,f'第13关已尝试 {len(attempts)} 组阵容、挑战 {battles} 次，仍未通关，自动推图已停止；装备与羁绊已归还，请前往游戏手动挑战')
    finally:
        client._automation_asset_owner = old_owner
        client._automation_asset_label = old_label
        client._automation_reserved_equipment = old_reserved | abyss_loadout.reserved_equipment(client,account_key)
