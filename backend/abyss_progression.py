"""Four-star progression with fresh rule teams and transactional borrowed assets."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
import random
import time

from . import config
from .game_client import GameError
from .abyss_state import metadata, enabled
from . import abyss_loadout, abyss_team
from .abyss_result import resolve_stars

ATTEMPT_LIMIT=5
# A server rejection during equipping may be repaired by isolating the rejected
# loan; allow a couple of fresh plans but never spin on an unchanged account.
APPLY_RECOVERY_LIMIT=2


def _fresh_clear(before, after):
    """Historical clear alone is insufficient evidence that this battle ended."""
    if not after or not after.get('cleared'):return False
    old=before or {};stars=after.get('stars')or[];previous=old.get('stars')or[]
    return (not old.get('cleared')
            or after.get('passCount',0)>old.get('passCount',0)
            or any(value and (i>=len(previous) or not previous[i]) for i,value in enumerate(stars)))


def run(client,params,*,stop_after_clear=False):
    from .tasks import _ABYSS_BATTLE_RE,normalize_abyss_config,abyss_scenes,item_name,_oid
    from .abyss_battle import run_abyss_battle,BattleUnconfirmed,check_battle_capacity
    auto_advance=enabled(params.get('autoAdvance'))
    cancelled=params.get('_cancel_event');callback=params.get('_on_progress');account=params.get('_account_key')
    recovery_warning=''
    excluded_equipment=set(params.get('_recovery_excluded_equipment')or[])
    excluded_artifacts=set(params.get('_recovery_excluded_artifacts')or[])
    isolated=abyss_loadout.protected_deferred_assets(client,account)
    excluded_equipment.update(isolated['equipment']);excluded_artifacts.update(isolated['artifacts'])
    def stopped():return cancelled is not None and cancelled.is_set()
    displayed_scenes = []
    displayed_scene = None
    def publish(**value):
        nonlocal displayed_scenes, displayed_scene
        sid = value.get('sceneId')
        # Refresh the compact picker data at stage/settlement boundaries, not
        # on every battle action. These are local server-confirmed records.
        if sid != displayed_scene or value.get('phase') in ('settled', 'finished'):
            displayed_scenes = abyss_scenes(client.account_state or {})
            displayed_scene = sid
        value['scenes'] = displayed_scenes
        if recovery_warning:value['recoveryWarning']=recovery_warning
        if callback:callback(value)
    def scene_record(scene_id):
        return next((row for row in abyss_scenes(client.account_state or {}) if row['id']==scene_id),None)
    def confirm(scene_id,before,*,unconfirmed=False,expect_win=False):
        for delay in (0,.5,1.5):
            if stopped():raise GameError('挑战已中止')
            if delay:
                if cancelled is not None:
                    if cancelled.wait(delay):raise GameError('挑战已中止')
                else:time.sleep(delay)
            try:client.bootstrap()
            except Exception:continue
            record=scene_record(scene_id)
            if record and (_fresh_clear(before,record) if unconfirmed else (not expect_win or record['cleared'])):
                return record
        raise GameError('服务器尚未确认本次新的战斗结算，已停止自动重试；请稍后刷新确认')
    selected=str(params.get('sceneId')or'')
    normalized=normalize_abyss_config(client.account_state or {},selected,params.get('waves'),auto_advance=auto_advance)
    automatic=normalized.get('autoConfigure',False)
    stages=sorted((sid for sid in metadata()['tables']['Scene'] if _ABYSS_BATTLE_RE.fullmatch(sid) and metadata()['tables']['Fantasy'].get(sid)),key=lambda sid:int(_ABYSS_BATTLE_RE.fullmatch(sid).group(1)))
    sequence=stages[stages.index(selected):] if auto_advance and not stop_after_clear else [selected]
    completed=[];lines=[];attempts=[];last_result={};last_scene=selected;previous=[];usage=Counter()
    def finish(ok,detail,recovery=False):
        publish(phase='finished',sceneId=last_scene,message=detail)
        return {**last_result,'ok':ok,'detail':detail,'lines':[*lines,detail],'completedScenes':completed,
                'lastSceneId':last_scene,'attempts':attempts,'autoConfigured':automatic,'recoveryRequired':recovery,
                'recoveryWarning':recovery_warning,'scenes':displayed_scenes}
    if abyss_loadout.pending(client,account):
        publish(phase='preparing',sceneId=selected,message='正在准备本次挑战')
        recovery=abyss_loadout.recover_before_challenge(client,account_key=account)
        if not recovery.get('restored') and not recovery.get('closed'):
            return finish(False,'上次借用装备/羁绊尚未完全归还：'+str(recovery.get('error')or'等待核对'),True)
        excluded_equipment.update(recovery.get('unresolvedEquipment')or[])
        excluded_artifacts.update(recovery.get('unresolvedArtifacts')or[])
        if not recovery.get('restored') or any((recovery.get('fallback')or{}).values()):
            recovery_warning=('上次可归还的装备与羁绊已归还；原角色已移除或归属冲突的物品保留在背包，缺失物品已记录。'
                              if recovery.get('restored') else '上次恢复记录格式异常，已保留到历史记录：'+str(recovery.get('error')or'格式异常'))
            lines.append(recovery_warning)
            publish(phase='preparing',sceneId=selected,message='正在继续本次挑战')
    for scene_id in sequence:
        last_scene=scene_id;label=f'第 {int(_ABYSS_BATTLE_RE.fullmatch(scene_id).group(1))} 关'
        stage_cleared=False;stage_four=False
        if not any(row['id']==scene_id for row in abyss_scenes(client.account_state or {})):
            return finish(bool(completed),f'自动推关结束：{label}尚未解锁或本期未开放，已通关 {len(completed)} 关')
        saved=next(row for row in abyss_scenes(client.account_state or {}) if row['id']==scene_id)
        if stop_after_clear and saved['cleared']:
            completed.append(scene_id)
            return finish(True,f'{label}已通关')
        best_stars=sum(bool(value) for value in saved.get('stars',[])[:4]) if saved['cleared'] else 0
        stage_cleared=best_stars>=1
        offensive=automatic
        if auto_advance and saved['cleared'] and sum(bool(value) for value in saved.get('stars',[])[:4])==4:
            completed.append(scene_id);lines.append(f'{label}已有 4 星结算，继续下一关');continue
        for attempt in range(1,(ATTEMPT_LIMIT if auto_advance else 1)+1):
            if stopped():return finish(False,'自动推关已中止，借用装备已归还')
            waves=deepcopy(normalized['waves']);context={'sceneId':scene_id,'attempt':attempt,'attemptLimit':ATTEMPT_LIMIT if auto_advance else 1,'autoConfigured':automatic,'offensive':offensive}
            failure=None;before=None;battle_attempted=False;building_team=False
            try:
                check_battle_capacity(client.account_state or {})
                if automatic:
                    building_team=True
                    publish(**context,phase='planning',message=f'{label}：优先关卡特殊效果，再选常规输出、生命/防御输出；破甲/关键克制手优先高速命中'+('，尝试替换上一轮角色' if attempt>1 else ''))
                    def planning(value):publish(**{**context,**value})
                    team=abyss_team.choose_teams(client.account_state or {},scene_id,previous=previous,usage=usage,cancelled=stopped,progress=planning,offensive=offensive)
                    waves=team['waves'];previous=[*waves['upper'],*waves['lower']];usage.update(previous)
                    context['waves']=waves;context['teamReasons']=team['reasons']
                    lines.extend(team['warnings'])
                    team_waves=waves;rejected_assets=0
                    while True:
                        publish(**context,phase='equipping',message=f'{label}：已选定八人，正在分配不重复的装备')
                        loadout=abyss_loadout.plan(client.account_state or {},team_waves,cancelled=stopped,progress=lambda value:publish(**{**context,**value}),
                                                 offensive=offensive,utility_roles=team.get('utilityRoles'),
                                                 exclude_equipment=excluded_equipment,exclude_artifacts=excluded_artifacts)
                        waves=abyss_team.arrange_waves(client.account_state or {},team_waves,targets=loadout.get('targets'),scene_id=scene_id)
                        context['waves']=waves
                        roster={_oid(r['_id']):r for r in (client.account_state or {}).get('RoleDataContainer',{}).get('Roles',[])}
                        protection=[]
                        for half,reason in team['reasons'].items():
                            if 'aoiProtected' not in reason:continue
                            rid=waves[half][3];name=item_name(roster[rid]['StaticID'])
                            reason['aoiProtected']={'roleId':rid,'name':name,'slot':4}
                            protection.append(f'{"上半" if half=="upper" else "下半"}4号位{name}由Aoi保护')
                        publish(**context,phase='equipping',message=f'{label}：输出配装与站位已确认'+('；'+'；'.join(protection) if protection else ''))
                        building_team=False
                        try:
                            abyss_loadout.apply(client,loadout,account_key=account,cancelled=stopped)
                            break
                        except Exception as error:
                            # A native No Role means the account still holds a loan
                            # for a hero instance the server no longer recognizes.
                            # Sync once, isolate that asset, then plan with others.
                            if not abyss_loadout.is_no_role(error) or rejected_assets>=APPLY_RECOVERY_LIMIT:
                                raise
                            rejected_assets+=1;building_team=True
                            publish(**context,phase='restoring',message=f'{label}：服务器拒绝配装（No Role），正在完整同步并隔离异常装备后重新配装')
                            recovery=abyss_loadout.isolate_rejected(client,[account],retry_deferred=False)
                            if not recovery.get('restored') and not recovery.get('closed'):
                                raise GameError('服务器拒绝配装后仍未确认账号状态，保留原始恢复记录：'+
                                                str(recovery.get('error')or'等待核对')) from error
                            excluded_equipment.update(recovery.get('equipment')or());excluded_artifacts.update(recovery.get('artifacts')or())
                            if any((recovery.get('deferred')or{}).values()):
                                recovery_warning=recovery.get('warning') or '部分借装未能归还，已隔离并保留原始归属，本次使用其他资产继续'
                                lines.append(recovery_warning)
                if stopped():raise GameError('挑战已中止，正在归还借用资产')
                battle_config=normalize_abyss_config(client.account_state or {},scene_id,waves)
                before=deepcopy(scene_record(scene_id));battle_attempted=True
                last_result=run_abyss_battle(client,{**params,**battle_config,'useSoul':True,'prioritizeSoulBreak':True,
                    '_on_progress':lambda value:publish(**{**context,'phase':'battle',**value})})
            except Exception as error:
                failure=error
            finally:
                if abyss_loadout.pending(client,account):
                    try:publish(**context,phase='restoring',message=f'{label}：正在归还借用装备与羁绊，请稍候')
                    except Exception:pass
                    # A server-confirmed rejection is isolated with its original
                    # owner instead of blocking every later stage; an unreadable
                    # account keeps the journal and stops the run.
                    try:
                        recovery=abyss_loadout.restore(client,account_key=account)
                        if not recovery['restored'] and abyss_loadout.pending(client,account):
                            recovery=abyss_loadout.isolate_unreturned(client,account,recovery,retry_deferred=False)
                    except Exception as error:
                        recovery={'restored':False,'error':str(error)}
                    if not recovery['restored']:
                        return finish(False,'装备/羁绊尚未完全归还，已停止推关：'+str(recovery.get('error')or'等待核对'),True)
                    excluded_equipment.update(recovery.get('unresolvedEquipment')or())
                    excluded_artifacts.update(recovery.get('unresolvedArtifacts')or())
                    if any((recovery.get('deferred')or{}).values()):
                        excluded_equipment.update(recovery['deferred'].get('equipment')or())
                        excluded_artifacts.update(recovery['deferred'].get('artifacts')or())
                        recovery_warning=recovery.get('warning') or '部分借装未能归还，已隔离并保留原始归属，本次使用其他资产继续'
                        lines.append(recovery_warning)
            if stopped():return finish(False,'自动推关已中止，借用装备已归还')
            recovered=isinstance(failure,BattleUnconfirmed) and battle_attempted
            if failure and not recovered:
                prefix='没有找到合适的阵容，过关失败。' if automatic and building_team else ''
                return finish(False,f'自动推关已停止：{label}，{prefix}{failure}')
            try:
                if recovered or not last_result.get('settlementConfirmed',False):
                    publish(**context,phase='checking',message=f'{label}：正在同步服务器，核对本次通关与星级')
                    record=confirm(scene_id,before,unconfirmed=recovered,expect_win=bool(last_result.get('ok')))
                else:record=scene_record(scene_id)
                if record is None:raise GameError('关卡结算状态无法核对，已停止自动重试')
                if recovered:
                    last_result={'ok':True,'settlementConfirmed':True,'recoveredSettlement':True,'detail':'连接结束后已从服务器确认通关'}
                    lines.append(f'{label}战斗连接中断，已同步确认新的通关进度，继续自动推关')
                won=bool(last_result.get('ok'));cleared=bool(record['cleared'])
                stars,star_source=resolve_stars(record.get('stars'),last_result.get('roundCount'),won=won) if won else (0,'failure')
                if stars is None:raise GameError('本次结算未提供完整星级或关卡计数，无法判断是否达到 4 星')
                if star_source=='counter' and stars==0:
                    won=False;cleared=False
                if won and not cleared:raise GameError('胜利尚未获得服务器通关确认，已停止自动重试')
                confirmed_stars=sum(bool(value) for value in record.get('stars',[])[:4]) if cleared else 0
                best_stars=max(best_stars,confirmed_stars,stars if cleared else 0)
                stage_cleared|=cleared and best_stars>=1;stage_four=won and cleared and stars==4
                attempts.append({'sceneId':scene_id,'attempt':attempt,'won':won,'stars':stars,'starSource':star_source,
                    'roundCount':last_result.get('roundCount'),'waves':deepcopy(waves),'recoveredSettlement':recovered,'offensive':offensive})
                lines.append(f'{label} 第 {attempt}/{ATTEMPT_LIMIT if auto_advance else 1} 次：{stars} 星，'+('本次已通关' if won and cleared else '本次未通关'))
                publish(**context,phase='settled',message=f'{label}已同步通关与星级，'+('准备继续' if auto_advance else '挑战结束'))
                if automatic and not won:offensive=True
            except Exception as error:
                if stopped():return finish(False,'自动推关已中止，借用装备已归还')
                return finish(False,f'自动推关已停止：{label}，{error}')
            if stop_after_clear and stage_cleared:
                completed.append(scene_id)
                return finish(True,f'{label}通关已由服务器确认，借用装备与羁绊已归还')
            if stage_four or not auto_advance:break
            if attempt<ATTEMPT_LIMIT:
                retry_reason='尚未通关' if stop_after_clear else '未达 4 星'
                publish(**{**context,'offensive':offensive},phase='retrying',message=f'{label}{retry_reason}，准备第 {attempt+1}/{ATTEMPT_LIMIT} 次挑战'+('；优先输出阵容和输出装备，破甲/关键克制手优先高速＋命中' if offensive else ''))
                if cancelled is not None:
                    if cancelled.wait(.3):return finish(False,'自动推关已中止，借用装备已归还')
                else:time.sleep(.3)
        if not stage_cleared:return finish(False,f'{label}：没有找到合适的阵容，过关失败。已尝试 {ATTEMPT_LIMIT if auto_advance else 1} 次，停止自动推关')
        completed.append(scene_id)
        if not stage_four and auto_advance:lines.append(f'{label}已获 {best_stars} 星通关，达到 {ATTEMPT_LIMIT} 次挑战上限，继续下一关')
        if not auto_advance:return finish(bool(last_result.get('ok')),last_result.get('detail') or '挑战完成')
        delay=config.get_request_delay_config()
        seconds=delay['ms']/1000*random.uniform(1-delay['jitter']/100,1+delay['jitter']/100) if delay['enabled'] else 0
        if cancelled is not None:
            if cancelled.wait(seconds):return finish(False,'自动推关已中止，借用装备已归还')
        elif seconds:time.sleep(seconds)
    return finish(True,f'自动推关完成：已通关 {len(completed)} 关，已到达最后一关')
