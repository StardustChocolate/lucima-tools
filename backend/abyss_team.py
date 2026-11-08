"""Fictional Abyss rule teams: pure account snapshots, no network or equipment mutation."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
from functools import lru_cache
import hashlib
import itertools
import json
import os
from pathlib import Path

from .abyss_state import metadata, fantasy_config, direct_properties, enabled
from .abyss_properties import _skill_setting, combine_functions, max_skill_level
from .game_client import GameError
from .tasks import _oid, item_name

WAVES = ('upper', 'lower')


def training_preference(role):
    """Purchased skill upgrades first, then awakening (training) stars.

    Count upgrades above level one, once per real visible skill/reference.
    Hidden follow-up skills do not invent extra upgrades. Star is promotion,
    while AwakenLV is the game's 调教觉醒 field.
    """
    tables=metadata()['tables'];definition=tables['Role'].get(role.get('StaticID'),{})
    def number(value,default=0):
        try:return max(0,int(value)) if not isinstance(value,bool) else default
        except (ValueError,TypeError,OverflowError):return default
    records={}
    for skill in (role.get('Skills')or{}).get('Skills')or[]:
        sid=skill.get('StaticID')
        if sid:records[sid]=max(records.get(sid,1),number(skill.get('Level'),1))
    levels={}
    for raw in str(definition.get('SkillIDs')or'').split('~'):
        sid,_,hidden=raw.partition(':')
        if not sid or hidden=='Hide':continue
        reference=tables['Skill'].get(sid,{}).get('ReferanceSkillID')or sid
        level=min(max_skill_level(sid,tables),max(1,records.get(reference,records.get(sid,1))))
        levels[reference]=max(levels.get(reference,0),level-1)
    return sum(levels.values()),number(role.get('AwakenLV'))


def _output_tier(combat):
    if combat['kind']=='healer':return 3
    if combat['kind']=='tank':return 2
    return int(combat['damageStat'] in ('HP','Defence'))


def element_matchup(attacker, defender):
    """Abyss selection preference: advantage 1, neutral 0, disadvantage -1.

    Water beats fire, fire beats earth, earth beats water. Light/dark are
    mutually advantageous; neither receives the reverse-counter penalty.
    """
    names={0:'Dark',1:'Fire',2:'Earth',3:'Ice',4:'Light'}
    def normalize(value):
        if isinstance(value,bool):return None
        if isinstance(value,int):return names.get(value)
        if value in names.values():return value
        return None
    a,d=normalize(attacker),normalize(defender)
    if a is None or d is None or a==d:return 0
    if {a,d}=={'Light','Dark'}:return 1
    beats={'Ice':'Fire','Fire':'Earth','Earth':'Ice'}
    if beats.get(a)==d:return 1
    if beats.get(d)==a:return -1
    return 0


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


@lru_cache(maxsize=1)
def catalog():
    root=Path(os.environ.get('ARK_ASSET_ROOT') or Path(__file__).resolve().parent.parent)
    from .resource_store import path as resource_path
    try:value=json.loads(resource_path('assets/abyss_auto_catalog.json').read_text(encoding='utf-8'))
    except (OSError,ValueError) as error:raise GameError('缺少深渊自动配队规则，请更新资源后重试') from error
    current=metadata()
    if value.get('schema')!=1 or any(value.get('tableSha256',{}).get(key)!=current['table_sha256'].get(key) for key in ('Fantasy','Status','Skill')):
        raise GameError('深渊自动规则与游戏资料不一致，请更新资源后重试')
    return value


def soul_functions(skill_id):
    raw=catalog()['soulSkills'].get(skill_id,{}).get('functions') or {}
    return raw.get('items',[]) if raw.get('__type__')=='sequence' else [raw]


def status_group(sid):
    return str(metadata()['tables']['Status'].get(sid,{}).get('Group') or sid)


def is_break(sid):
    row=metadata()['tables']['Status'].get(sid,{})
    prop=direct_properties(row.get('Dynamic',{}))
    return sid=='ReduceDef' or status_group(sid)=='ReduceDef' or float(prop.get('DefenceRate',0) or 0)<0


def skill_functions(role, skill, *, soul=False, passive=False):
    functions=_skill_setting(role,skill,metadata()['tables'],passive=passive).functions
    return combine_functions([*functions,*(soul_functions(skill['StaticID']) if soul else [])])


def skill_break(role, skill, *, soul=False, ignore=False):
    for node in walk(skill_functions(role,skill,soul=soul)):
        if node.get('Fun')=='AddStatus' and node.get('TargetCamp','Enemy')!='My' and is_break(str(node.get('FID')or'')):
            if not ignore or float(node.get('IgnoreResist',0)or 0)>=1:
                return True
    return False


def skill_status(role, skill, group, *, soul=False, ignore=False):
    """Selectable enemy status, including level and soul additions."""
    definition=metadata()['tables']['Skill'].get(skill['StaticID'],{})
    for node in capability_nodes(skill_functions(role,skill,soul=soul),role):
        if node.get('Fun')!='AddStatus' or float(node.get('Rate',1) or 0)<=0:continue
        if node.get('TargetCamp',definition.get('TargetCamp') or 'Enemy')=='My' or node.get('Target')=='Self':continue
        if status_group(str(node.get('FID')or''))==group and (not ignore or float(node.get('IgnoreResist',0)or 0)>=1):return True
    return False


def capability_nodes(functions,role):
    """Follow status/additional-skill references with cycle guards; capabilities are conditional."""
    tables=metadata()['tables'];seen=set();pending=[(functions,0)]
    while pending:
        value,depth=pending.pop()
        for node in walk(value):
            yield node
            if depth>=5:continue
            fid=node.get('FID')
            if not isinstance(fid,str):continue
            kind='Status' if node.get('Fun')=='AddStatus' else 'Skill' if node.get('Fun') in ('AdditionSkill','CounterSkill','AddPinchSkill','ReplaceSkill') else None
            if kind is None or (kind,fid) in seen:continue
            seen.add((kind,fid))
            if kind=='Status':pending.append((direct_properties(tables['Status'].get(fid,{}).get('Dynamic',{})),depth+1))
            elif fid in tables['Skill']:pending.append((skill_functions(role,{'StaticID':fid,'Level':1}),depth+1))


def role_traits(role, *, seal_passive=False):
    tables=metadata()['tables'];sid=str(role.get('StaticID')or'');row=tables['Role'].get(sid,{})
    levels={s.get('StaticID'):s for s in (role.get('Skills')or{}).get('Skills')or[]}
    result={'buffs':set(),'buffTargets':{},'debuffs':set(),'flags':set(),'immune':set(),'break':False,'soulBreak':False,'heal':False,'recovery':False,'protection':False,
            'blockBuff':False,'antiHeal':False,'soulBlockBuff':False,'soulAntiHeal':False,
            'cleanse':False,'strip':False,'passive':False,'nonAttack':False,'aoe':False,'scale':'Attack',
            'forceConquer':False,'favorableSkills':set()}
    skills=[(s.split(':')[0],':Hide' in s) for s in str(row.get('SkillIDs')or'').split('~') if s]
    for skill_id,hidden in skills:
        definition=tables['Skill'].get(skill_id,{})
        passive=definition.get('SkillType')=='Passive'
        result['passive'] |= passive
        if seal_passive and passive:continue
        record=levels.get(skill_id) or {'StaticID':skill_id,'Level':1}
        funcs=skill_functions(role,record)
        if not passive and not hidden:
            result['break'] |= skill_break(role,record)
            result['soulBreak'] |= skill_break(role,record,soul=True,ignore=True)
            for flag,group in (('blockBuff','CannotBuff'),('antiHeal','Unhealable')):
                result[flag] |= skill_status(role,record,group)
                result['soul'+flag[0].upper()+flag[1:]] |= skill_status(role,record,group,soul=True,ignore=True)
            result['nonAttack'] |= definition.get('TargetCamp')=='My'
            result['aoe'] |= definition.get('TargetType')=='All' and definition.get('TargetCamp')!='My'
            if definition.get('TargetCamp')!='My' and definition.get('TargetType','One') in ('','One'):result['flags'].add('SingleSkill')
        for node in capability_nodes(funcs,role):
            fun=node.get('Fun')
            if not hidden and node.get('Trigger')=='ForceConquer' and (passive or definition.get('TargetCamp')!='My'):
                result['forceConquer']=True;result['favorableSkills'].add(skill_id)
            friendly=node.get('TargetCamp',definition.get('TargetCamp') or 'Enemy')=='My' or node.get('Target')=='Self'
            immunities=node.get('AntiBuffGs')or[]
            result['immune'].update(immunities.split('~') if isinstance(immunities,str) else immunities if isinstance(immunities,list) else [])
            if isinstance(fun,str) and fun:result['flags'].add(fun)
            if 'FixedDamage' in str(node.get('Tags')or''):result['flags'].add('FixedDamage')
            if 'PenetrateRate' in node:result['flags'].add('Penetrate')
            modifier=node.get('ConditionAttack')
            if isinstance(modifier,dict):
                if modifier.get('Property')=='Speed':result['flags'].add('SpeedScale')
                if modifier.get('Property')=='LastHPRate':result['flags'].add('LostHPScale')
            if fun in ('AddHP','Revive','Resurrection') and friendly:
                result['recovery']=True
                if node.get('Target')!='Self':result['heal']=True
            if fun=='RemoveDeBuf' and node.get('TargetCamp')=='My':result['cleanse']=True
            if fun=='RemoveBuf' and node.get('TargetCamp','Enemy')!='My':result['strip']=True
            if fun=='AddStatus':
                fid=str(node.get('FID')or'');group=status_group(fid)
                target='buffs' if friendly else 'debuffs'
                result[target].update((fid,group))
                if friendly:
                    # A self-only shield on a support does not shield our damage
                    # dealers. Keep who can receive it for team-level coverage.
                    scope=node.get('Target')
                    if not scope:
                        scope=(definition.get('TargetType') or 'One') if definition.get('TargetCamp')=='My' else 'Self'
                    scope='self' if scope in ('Self','Source') else 'ally'
                    for key in (fid,group):result['buffTargets'].setdefault(key,set()).add(scope)
                    properties=direct_properties(tables['Status'].get(fid,{}).get('Dynamic',{}))
                    result['protection'] |= group in ('Shield','Invincible','Immortal','SkillNullifier') or bool(properties.get('Shield') or properties.get('Invincible'))
                    result['recovery'] |= group in ('Heal','ContinuousHeal','Lifesteal','Revive')
            const=node.get('ConstAttack')
            if isinstance(const,dict) and const.get('Property') in ('Defence','MaxHP'):
                result['scale']='HP' if const['Property']=='MaxHP' else 'Defence'
    return result


def combat_role(role, *, traits=None, options=None):
    """Classify damage by actual self-scaling skills, not the job or a heal alone."""
    from equipment import recommend
    tables=metadata()['tables'];sid=str(role.get('StaticID')or'');definition=tables['Role'].get(sid,{})
    traits=traits if traits is not None else role_traits(role)
    options=options if options is not None else recommend.profiles(role)
    damage=next((p for p in options if 'damage' in p['priorities']),None)
    records={s['StaticID']:s for s in (role.get('Skills')or{}).get('Skills',[]) if s.get('StaticID')}
    attacks=[];scales=set();protects_back=False
    for index,raw in enumerate(str(definition.get('SkillIDs')or'').split('~')):
        skill_id,_,hidden=raw.partition(':');skill=tables['Skill'].get(skill_id,{})
        if not skill_id:continue
        record=records.get(skill_id) or {'StaticID':skill_id,'Level':1}
        functions=skill_functions(role,record)
        nodes=list(capability_nodes(functions,role))
        if sid=='H119' and skill_id=='H119S2':
            protects_back=any(node.get('Target')=='BackRow' for node in nodes)
        for node in nodes:
            const=node.get('ConstAttack')
            if isinstance(const,dict) and const.get('Who','Source') in ('','Source','Self') and float(const.get('Rate',0)or 0)>0:
                if const.get('Property') in ('MaxHP','HP'):scales.add('HP')
                if const.get('Property')=='Defence':scales.add('Defence')
        if skill.get('SkillType')!='Passive' and hidden!='Hide' and skill.get('TargetCamp')!='My':
            attacks.append((index,skill_id,str(skill.get('TargetType')or'One')))
        # Counter/extra damage is part of the real output, including a defence
        # caster whose visible third skill is a friendly support cast.
        for node in nodes:
            fid=node.get('FID');extra=tables['Skill'].get(fid,{})
            if node.get('Fun') in ('AdditionSkill','CounterSkill','ReplaceSkill') and extra and extra.get('TargetCamp')!='My':
                attacks.append((index+1,str(fid),str(extra.get('TargetType')or'One')))
    scale='Defence' if 'Defence'in scales else 'HP' if 'HP'in scales else (damage or options[0])['damageStat']
    job=definition.get('Job')
    if job=='Medic':kind='healer'
    elif not attacks or (not scales and (job=='Defender' or damage is None)):kind='tank'
    else:
        primary=max(attacks,key=lambda row:(row[0],row[1]))
        kind='single' if primary[2] in ('','One','RandomOne','HighestAtkOne','HightestAtkOne') else 'area'
    profile=deepcopy(damage or options[0])
    profile.update(id='abyss-output',title={'HP':'生命输出','Defence':'防御输出','Attack':'攻击输出'}[scale],
                   priorities=['damage','Speed',scale],damageStat=scale,ranking=None,hardConditions=[],hitTarget=False,metricLabel='输出参考分')
    return {'kind':kind,'tier':{'single':0,'area':1,'tank':2,'healer':3}[kind],
            'damageStat':scale,'selfScaling':bool(scales),'profile':profile,'protectsBack':protects_back}


def _arrange_group(group, *, position_damage=False, protect_back=False):
    group=sorted(group,key=lambda r:(-r['panel']['HP']*(1+r['panel']['Defence']/300),r['id']))
    guardians=[r for r in group if r['sid']=='H119'] if protect_back else []
    if guardians:
        candidates=[r for r in group if r['sid']!='H119' and r['combat']['kind'] in ('single','area')]
        candidates=candidates or [r for r in group if r['sid']!='H119']
        highest=max(r['output'] for r in candidates)
        strong=[r for r in candidates if r['output']>=highest*.8]
        # Only compare high-output allies; protect the fragile one among them.
        target=max(strong,key=lambda r:(r['output']/max(1,r['panel']['HP']*(1+r['panel']['Defence']/300))**.5,
                                        r['output'],r['id']))
        group.remove(target);group.append(target)
    elif position_damage:
        target=max(group,key=lambda r:(r['output'],r['id']));group.remove(target);group.append(target)
    return group


def arrange_waves(state,waves,*,targets=None,scene_id=None):
    """Re-evaluate Aoi's protected slot with the final planned outfits."""
    from equipment import plans,stats,recommend
    roles={_oid(r['_id']):r for r in (state.get('RoleDataContainer')or{}).get('Roles',[])}
    equips=(state.get('EquipmentContainer')or{}).get('Datas',[]);by_id={_oid(e['_id']):e for e in equips}
    settings=fantasy_config(scene_id,state)['Waves'] if scene_id else [{},{}]
    output={}
    for index,wave in enumerate(WAVES):
        policy=wave_policy(settings[index]);group=[];protect=False
        for rid in waves[wave]:
            role=roles[rid];combat=combat_role(role)
            gear=[by_id[eid] for eid in targets[rid]] if targets is not None else plans.worn_items(equips,rid)[0]
            panel=stats.role_panel(state,role,gear,include_artifact_effects=True)['panel']
            group.append({'id':rid,'sid':role['StaticID'],'panel':panel,'combat':combat,'output':recommend.metric(panel,{**combat['profile'],'priorities':['damage']})})
            protect |= combat['protectsBack'] and 'SealPassive' not in policy['flags']
        output[wave]=[r['id'] for r in _arrange_group(group,position_damage=policy['positionDamage'],protect_back=protect)]
    return output


def _conditions(definition):
    for node in walk(definition):
        cond=node.get('Condition')
        if isinstance(cond,dict):yield cond
        for cond in node.get('Conditions')or[]:
            if isinstance(cond,dict):yield cond


def wave_policy(wave):
    """Compile every active effect by actual selector/property, never translated text."""
    rules={'requiredJobs':{},'requiredElements':{},'forbiddenJobs':set(),'forbiddenElements':set(),
           'preferredJobs':set(),'preferredElements':set(),'buffs':set(),'debuffs':set(),'antiDebuffs':set(),
           'flags':set(),'desiredFlags':set(),'positionDamage':False,'effects':[]}
    table=metadata()['tables']['Status'];known=catalog()['effects']
    for kind in ('Bs','DBs'):
        for entry in (wave.get('Buffs')or{}).get(kind)or[]:
            sid=entry['B'];definition=table.get(sid)
            if sid not in known or not definition or known[sid]['definitionSha256']!=hashlib.sha256(json.dumps(definition,sort_keys=True).encode()).hexdigest():
                raise GameError(f'关卡效果 {sid} 尚未核对自动配队规则，可改用手动队伍')
            good=kind=='Bs';own=entry.get('Target')!='Enemy'
            rules['effects'].append(sid)
            for cond in _conditions(definition):
                prop,fun,key=cond.get('Property'),cond.get('CheckFun'),cond.get('CheckID')
                if prop in ('CampJobCount','CampElementCount'):
                    if cond.get('Comparer','Gt') not in ('Gt','Gtn','Eq') or cond.get('Invert'):
                        raise GameError(f'关卡效果 {sid} 的人数条件尚不支持自动配队')
                    limit=int(cond.get('Value',1))+(cond.get('Comparer')=='Gtn')
                    if good and own:
                        target=rules['requiredJobs' if prop=='CampJobCount' else 'requiredElements'];target[key]=max(target.get(key,0),limit)
                    elif not good and own:
                        if limit!=1:raise GameError(f'关卡效果 {sid} 需要更新职业排除规则')
                        rules['forbiddenJobs' if prop=='CampJobCount' else 'forbiddenElements'].add(key)
                if good:
                    if fun in ('RoleJob','FirstJob'):rules['preferredJobs'].add(key)
                    if fun=='RoleElement':rules['preferredElements'].add(key)
                    if fun in ('HaveBuff','HaveBuffGroup'):
                        rules['buffs' if own else 'debuffs'].add(key)
                    if prop=='BuffGroupStack' and key=='Immortal':rules['buffs'].add('Immortal')
                    if fun=='Position' and str(key)=='3':rules['positionDamage']=True
                    for check_id,flag in [('IsAddition','AdditionSkill'),('IsCounter','CounterSkill'),('IsUseSoul','Soul'),('TargetIsAll','AOE'),('IsDamageSkill','NonAttack'),('IsNormalSkill','SingleSkill')]:
                        if fun==check_id and ((fun in ('IsDamageSkill','IsNormalSkill'))==bool(cond.get('Invert'))):rules['desiredFlags'].add(flag)
                    if fun=='FunHasTag' and key=='FixedDamage':rules['desiredFlags'].add('FixedDamage')
            props=direct_properties(definition.get('Dynamic',{}))
            if not good and own:
                if props.get('SealPassive'):rules['flags'].add('SealPassive')
                for value in ([props.get('AntiFun')] if isinstance(props.get('AntiFun'),str) else props.get('AntiFun')or[]):rules['flags'].add(value)
                if props.get('NoCounter'):rules['flags'].add('CounterSkill')
                if float(props.get('AddFightSPRate',0)or 0)<0 or float(props.get('AddFocusSPRate',0)or 0)<0:rules['flags'].add('NoSP')
                if sid=='FB_Special_006':rules['flags'].add('PunishNonAttack')
                if sid=='FB_Special_005':rules['flags'].add('ReduceHeal')
                if sid=='FB_Special_014':rules['flags'].add('NeedCleanse')
                if sid=='FB_Special_015':rules['flags'].add('NoSoul')
            if entry.get('Target')=='Enemy':
                if props.get('AntiReduceSpeed'):rules['antiDebuffs'].add('ReduceSpeed')
                raw=props.get('AntiBuffGs')or[];rules['antiDebuffs'].update(raw.split('~') if isinstance(raw,str) else raw)
            if good:
                if sid=='FG_Special_015':rules['desiredFlags'].add('HPScale')
                if sid=='FG_Special_016':rules['desiredFlags'].add('SpeedScale')
                if sid=='FG_Special_020':rules['desiredFlags'].add('LostHPScale')
                if sid=='FG_Special_025':rules['desiredFlags'].add('Penetrate')
                if sid=='FG_Special_007':rules['desiredFlags'].add('SP')
    # Medical slots are a user constraint, not a per-provider score bonus.
    # FirstJob/RoleJob only need one provider; repeated effects do not add slots.
    medic_count=max(rules['requiredJobs'].get('Medic',0),int('Medic' in rules['preferredJobs']))
    rules['medicCount']=medic_count
    if medic_count:rules['requiredJobs']['Medic']=medic_count
    return rules


def _enemy_traits(wave):
    traits={'buffs':set(),'debuffs':set(),'flags':set(),'immune':set(),'recovery':False,'protection':False}
    rows=[]
    monsters=wave.get('Monsters')or[]
    bosses=[monster for monster in monsters if enabled(monster.get('Boss'))]
    for monster in bosses or monsters:
        row=metadata()['tables']['Role'].get(monster['M'],{})
        if not row:raise GameError(f'缺少敌人 {monster["M"]} 的资料')
        capability=role_traits({'StaticID':monster['M'],'LV':60})
        for key in traits:
            if isinstance(traits[key],set):traits[key].update(capability[key])
            else:traits[key] |= capability[key]
        rows.append({'StaticID':monster['M'],'Element':row.get('Element'),'isBoss':enabled(monster.get('Boss'))})
    for kind in ('Bs','DBs'):
        for entry in (wave.get('Buffs')or{}).get(kind)or[]:
            if entry.get('Target')!='Enemy':continue
            props=direct_properties(metadata()['tables']['Status'].get(entry.get('B'),{}).get('Dynamic',{}))
            for node in walk(props):
                traits['protection'] |= bool(node.get('Shield') or node.get('Invincible')) or (node.get('Fun')=='AddStatus' and status_group(str(node.get('FID')or'')) in ('Shield','Invincible','Immortal'))
                traits['recovery'] |= node.get('Fun') in ('AddHP','Revive','Resurrection')
                immune=node.get('AntiBuffGs')or[]
                traits['immune'].update(immune.split('~') if isinstance(immune,str) else immune if isinstance(immune,list) else [])
    return traits,rows


def _stage_coverage(group,rule):
    """Reward an enabled condition once per team, never once per provider.

    One ally-shield provider enables the damage dealers. More providers of the
    same shield do not earn extra stage points and crowd them out. Self buffs
    still count when the recipient is an actual damage role.
    """
    damage=[r for r in group if r['combat']['kind'] in ('single','area')]
    buffs=set()
    for key in rule['buffs']:
        for role in group:
            if key not in role['traits']['buffs']:continue
            scopes=role['traits'].get('buffTargets',{}).get(key,{'self'})
            if (role in damage or ('ally' in scopes and any(r['id']!=role['id'] for r in damage))):
                buffs.add(key);break
    debuffs={key for key in rule['debuffs']-rule['antiDebuffs']
             if any(key in r['traits']['debuffs'] for r in group)}
    cleanse='NeedCleanse' in rule['flags'] and any(r['traits']['cleanse'] for r in group)
    return 9*(len(buffs)+len(debuffs))+8*bool(cleanse)


PREFERRED_DAMAGE_HEROES = frozenset({'H165', 'H146'})  # 大哥、老板娘；官方角色资料别名。


def preferred_main(role, rule, traits):
    """User's Lv60 mains take precedence over bonuses, never over restrictions."""
    definition = metadata()['tables']['Role'].get(role.get('StaticID'), {})
    if role.get('StaticID') not in PREFERRED_DAMAGE_HEROES or int(role.get('LV') or 0) != 60:
        return False
    if definition.get('Job') in rule['forbiddenJobs'] or definition.get('Element') in rule['forbiddenElements']:
        return False
    flags = rule['flags']
    return not (flags.intersection(traits['flags'])
                or ('SealPassive' in flags and traits['passive'])
                or ('NoSP' in flags and definition.get('SPType'))
                or ('PunishNonAttack' in flags and traits['nonAttack']))


def selection_state(state):
    """Rank heroes independently of their bonds, which automatic loadouts replace.

    In particular, an unrecognized currently worn bond must not reject a usable
    hero before the planner can lend a known replacement.
    """
    return {**state, 'Artifacts': {'Datas': []}}


def choose_teams(state,scene_id,*,previous=(),usage=None,cancelled=None,progress=None,offensive=True):
    from equipment import stats,plans,recommend
    config=fantasy_config(scene_id,state);usage=usage or {};previous=set(previous)
    ranking_state=selection_state(state)
    roles=[r for r in (state.get('RoleDataContainer')or{}).get('Roles')or[] if int(r.get('LV')or 0)==60 and _oid(r.get('_id')) and stats.role_base(r)]
    if len({r['StaticID'] for r in roles})<8:raise GameError('自动配队至少需要 8 名不同的满级（Lv60）角色')
    candidates=[];policies=[];warnings=[];counter_needs=[];bosses=[]
    for wave_index,wave in enumerate(config['Waves']):
        rule=wave_policy(wave);policies.append(rule);enemy_traits,enemies=_enemy_traits(wave)
        medic_count=rule['medicCount']
        half='上半' if wave_index==0 else '下半'
        if medic_count>4 or (medic_count and 'Medic' in rule['forbiddenJobs']):
            raise GameError(f'{half}医疗人数要求与关卡限制冲突，请改用手动队伍')
        boss_targets=[enemy for enemy in enemies if enemy['isBoss']] or enemies
        bosses.append(boss_targets)
        needs=set()
        immunity=rule['antiDebuffs']|enemy_traits['immune']
        if enemy_traits['protection'] and 'CannotBuff' not in immunity:needs.add('blockBuff')
        if enemy_traits['recovery'] and 'Unhealable' not in immunity:needs.add('antiHeal')
        if enemy_traits['protection'] or 'Immune' in enemy_traits['buffs']:needs.add('strip')
        counter_needs.append(needs)
        choices=[]
        for role in roles:
            definition=metadata()['tables']['Role'][role['StaticID']];job,element=definition.get('Job'),definition.get('Element')
            if job in rule['forbiddenJobs'] or element in rule['forbiddenElements']:continue
            if job=='Medic' and not medic_count:continue
            traits=role_traits(role,seal_passive='SealPassive' in rule['flags'])
            if 'ReduceDef' in immunity:traits['break']=False;traits['soulBreak']=False
            worn,_=plans.worn_items((state.get('EquipmentContainer')or{}).get('Datas')or[],_oid(role['_id']))
            try:panel=stats.role_panel(ranking_state,role,worn,include_artifact_effects=True)['panel']
            except GameError:continue
            info=recommend.profiles(role);combat=combat_role(role,traits=traits,options=info);profile=combat['profile']
            for flag,group in (('blockBuff','CannotBuff'),('antiHeal','Unhealable')):
                if group in immunity:traits[flag]=False;traits['soul'+flag[0].upper()+flag[1:]]=False
                else:traits[flag] |= traits['soul'+flag[0].upper()+flag[1:]]
            quality=min(30,recommend.metric(panel,profile)/8)
            matchups=[element_matchup(element,enemy.get('Element')) for enemy in boss_targets]
            # An available ForceConquer skill removes the selection penalty;
            # other skills are not falsely claimed to have favorable damage.
            if traits.get('forceConquer'):matchups=[max(0,value) for value in matchups]
            preferred=preferred_main(role,rule,traits)
            if preferred:matchups=[0 for value in matchups]
            # Shared buff/debuff prerequisites are scored by whole-team
            # coverage below. Individual damage mechanisms remain per role.
            desirable=0
            for flag in rule['desiredFlags']:
                desirable+=8*bool((flag in traits['flags']) or (flag=='NonAttack' and traits['nonAttack']) or (flag=='AOE' and traits['aoe']) or (flag=='HPScale' and traits['scale']=='HP') or (flag=='SP' and definition.get('SPType')))
            penalty=sum(10 for flag in rule['flags'] if flag in traits['flags'])
            if 'SealPassive' in rule['flags'] and traits['passive']:penalty+=6
            if 'NoSP' in rule['flags'] and definition.get('SPType'):penalty+=15
            if 'PunishNonAttack' in rule['flags'] and traits['nonAttack']:penalty+=20
            if 'ReduceHeal' in rule['flags'] and traits['heal']:penalty+=8
            support=(8*traits['cleanse'] if enemy_traits['debuffs'] else 0)+6*traits['strip']*bool(enemy_traits['buffs'])
            rule_fit=desirable-penalty+4*(job in rule['preferredJobs'])+4*(element in rule['preferredElements'])
            value=quality+rule_fit+support
            skill_upgrades,awaken=training_preference(role)
            choices.append({'id':_oid(role['_id']),'sid':role['StaticID'],'job':job,'element':element,'traits':traits,'score':value,
                            'output':recommend.metric(panel,{**profile,'priorities':['damage']}),'panel':panel,'worn':len(worn),'combat':combat,
                            'advantage':sum(value>0 for value in matchups),'disadvantage':sum(value<0 for value in matchups),'ruleFit':rule_fit,
                            'outputTier':_output_tier(combat),'skillUpgrades':skill_upgrades,'awaken':awaken,'preferredMain':preferred})
        if len({r['sid'] for r in choices})<4:raise GameError(f'{"上半" if wave_index==0 else "下半"}排除受罚职业/属性后，不足 4 名合法满级角色')
        choices.sort(key=lambda r:(not r['preferredMain'],-r['ruleFit'],r['outputTier'],not r['traits'].get('forceConquer'),r['combat']['tier'],-r['skillUpgrades'],-r['awaken'],-r['score'],r['id']))
        distinct={}
        for role in choices:distinct.setdefault(role['sid'],role)
        if sum(r['job']=='Medic' for r in distinct.values())<medic_count:
            raise GameError(f'{half}需要恰好 {medic_count} 名医疗，符合关卡限制的满级医疗不足')
        pool=list(distinct.values())[:20]
        # Keep elemental alternatives even when a large roster's first twenty
        # have higher current panels. Existing role/utility priorities still win.
        for tier in range(4):
            same_tier=[r for r in distinct.values() if r['combat']['tier']==tier]
            pool += [r for r in same_tier if not r['disadvantage'] and r['advantage']][:8]
            pool += [r for r in same_tier if not r['disadvantage'] and not r['advantage']][:8]
        for key,values in [('job',set(rule['requiredJobs'])|rule['preferredJobs']),('element',set(rule['requiredElements'])|rule['preferredElements'])]:
            for value in sorted(values):pool += [r for r in choices if r[key]==value][:8]
        pool += [r for r in choices if r['traits']['break']][:12]
        for flag in sorted(needs):pool += [r for r in choices if r['traits'][flag]][:8]
        for key in sorted(rule['buffs']):pool += [r for r in choices if key in r['traits']['buffs']][:12]
        for key in sorted(rule['debuffs']-rule['antiDebuffs']):pool += [r for r in choices if key in r['traits']['debuffs']][:8]
        if 'NeedCleanse' in rule['flags']:pool += [r for r in choices if r['traits']['cleanse']][:8]
        pool += [r for r in choices if r['traits'].get('forceConquer') and r['combat']['kind'] in ('single','area')][:12]
        pool=list({r['id']:r for r in pool}.values());pool.sort(key=lambda r:r['id'])
        # Camp-count conditions in Bs grant bonuses; they are not entry bans.
        # Keep the existing bonus-first policy unless the user's priority mains
        # are available, in which case their slots may forgo those bonuses.
        favor_mains=any(r['preferredMain'] for r in choices)
        required=lambda group: sum(r['job']=='Medic' for r in group)==medic_count and (favor_mains or (all(sum(r['job']==k for r in group)>=n for k,n in rule['requiredJobs'].items()) and all(sum(r['element']==k for r in group)>=n for k,n in rule['requiredElements'].items())))
        def value(group):
            fulfil=sum(min(n,sum(r['job']==k for r in group))/n for k,n in rule['requiredJobs'].items())+sum(min(n,sum(r['element']==k for r in group))/n for k,n in rule['requiredElements'].items())
            breaker=any(r['traits']['break'] for r in group)
            counters=sum(any(r['traits'][flag] for r in group) for flag in needs)
            dual=any(r['traits']['break'] and any(r['traits'][flag] for flag in needs) for r in group)
            # Self penetration (e.g. 大哥) improves single-target output but
            # never replaces the team's real ReduceDef coverage above.
            damage_break=any((r['traits']['break'] or 'Penetrate' in r['traits']['flags'])
                             and r['combat']['kind']=='single' and r['outputTier']==0 for r in group)
            favorable=sum(bool(r['traits'].get('forceConquer')) and r['combat']['kind'] in ('single','area') for r in group)
            # Special effects come before all role, breaker and growth preferences.
            preferred_count=sum(r['preferredMain'] for r in group)
            return (int(preferred_count>0),preferred_count,fulfil,sum(r['ruleFit'] for r in group)+_stage_coverage(group,rule),breaker,counters,
                    -sum(r['combat']['kind']=='healer' for r in group),-sum(r['combat']['kind']=='tank' for r in group),
                    favorable,-sum(r['outputTier']==1 for r in group),-sum(r['combat']['kind']=='area' for r in group),damage_break,dual,
                    int(any(r['combat']['protectsBack'] for r in group) and 'SealPassive' not in rule['flags']),
                    sum(r['id'] not in previous for r in group),
                    -sum(r['disadvantage'] for r in group),sum(r['advantage'] for r in group),
                    sum(r['skillUpgrades'] for r in group),sum(r['awaken'] for r in group),
                    sum(r['score']-usage.get(r['id'],0)*2 for r in group))
        beam=[((),-1)]
        for size in range(4):
            expanded=[]
            for group,last in beam:
                for index in range(last+1,len(pool)):
                    if cancelled and cancelled():raise GameError('自动配队已中止')
                    role=pool[index]
                    if any(r['sid']==role['sid'] for r in group):continue
                    next_group=(*group,role);remaining=3-size
                    medics=sum(r['job']=='Medic' for r in next_group)
                    if medics>medic_count or medics+remaining<medic_count:continue
                    if not favor_mains and any(sum(r['job']==k for r in next_group)+remaining<n for k,n in rule['requiredJobs'].items()):continue
                    if not favor_mains and any(sum(r['element']==k for r in next_group)+remaining<n for k,n in rule['requiredElements'].items()):continue
                    if size==3 and not required(next_group):continue
                    expanded.append((next_group,index))
            ranked=sorted(expanded,key=lambda row:value(row[0]),reverse=True)
            beam=ranked[:256]
            # 保留不占用热门核心的替代，避免两半各自择优后无法凑成八人。
            popular=Counter(r['id'] for group,_ in beam for r in group).most_common(8)
            for rid,_ in popular:
                beam += list(itertools.islice((entry for entry in ranked if all(r['id']!=rid for r in entry[0])),32))
            beam += list(itertools.islice((entry for entry in ranked if not any(r['traits']['break'] for r in entry[0])),32))
            beam=list({tuple(r['id'] for r in group):(group,last) for group,last in beam}.values())
            if progress:progress({'phase':'planning','sceneId':scene_id,'wave':wave_index,'message':f'{"上半" if wave_index==0 else "下半"}：优先关卡特殊效果，再选常规输出与高速破甲核心；同条件比较技能升级和调教星数'})
        if not beam:raise GameError(f'{"上半" if wave_index==0 else "下半"}无法同时满足关卡职业/属性增益与禁用条件')
        candidates.append([(group,value(group)) for group,_ in beam])
    best=None
    for upper,uv in candidates[0]:
        us={r['sid'] for r in upper}
        for lower,lv in candidates[1]:
            if us.intersection(r['sid'] for r in lower):continue
            key=(*(uv[i]+lv[i] for i in range(len(uv)-1)),min(uv[-1],lv[-1]),uv[-1]+lv[-1])
            if best is None or key>best[0]:best=(key,(upper,lower))
    if best is None:raise GameError('上下半无法组成互不重复的八人队伍，请培养更多符合规则的满级角色')
    output={};reasons={};utilities={};breaker_roles={}
    for index,(wave,group) in enumerate(zip(WAVES,best[1])):
        protect=any(r['combat']['protectsBack'] for r in group) and 'SealPassive' not in policies[index]['flags']
        group=_arrange_group(group,position_damage=policies[index]['positionDamage'],protect_back=protect)
        output[wave]=[r['id'] for r in group]
        reasons[wave]={'breakers':[item_name(r['sid']) for r in group if r['traits']['break']], 'effects':policies[index]['effects']}
        reasons[wave]['roleTypes']={kind:[item_name(r['sid']) for r in group if r['combat']['kind']==kind] for kind in ('single','area','tank','healer')}
        reasons[wave]['training']={r['id']:{'skillUpgrades':r['skillUpgrades'],'awakenStars':r['awaken']} for r in group}
        reasons[wave]['preferredMains']=[item_name(r['sid']) for r in group if r['preferredMain']]
        reasons[wave]['medicCount']={'required':policies[index]['medicCount'],'selected':sum(r['job']=='Medic' for r in group)}
        reasons[wave]['favorableElementSkills']={r['id']:sorted(r['traits'].get('favorableSkills',())) for r in group if r['traits'].get('forceConquer')}
        reasons[wave]['stageCoverage']={'score':_stage_coverage(group,policies[index]),
            'note':'同一增益或负面状态条件按队伍覆盖一次，重复辅助不额外加分；其余位置优先输出'}
        reasons[wave]['bossElements']=[enemy.get('Element') for enemy in bosses[index]]
        reasons[wave]['elementMatchups']={
            'advantaged':[item_name(r['sid']) for r in group if r['advantage'] and not r['disadvantage']],
            'neutral':[item_name(r['sid']) for r in group if not r['advantage'] and not r['disadvantage']],
            'disadvantaged':[item_name(r['sid']) for r in group if r['disadvantage']]}
        if reasons[wave]['elementMatchups']['disadvantaged']:
            warnings.append(f'{"上半" if index==0 else "下半"}优先满足原有输出、破甲与关卡规则，属性选择不足时使用：'+ '、'.join(reasons[wave]['elementMatchups']['disadvantaged'])+'（被Boss属性克制）')
        if protect:reasons[wave]['aoiProtected']={'roleId':group[-1]['id'],'name':item_name(group[-1]['sid']),'slot':4}
        breakers=[r for r in group if r['traits']['break']]
        if breakers:
            primary=max(breakers,key=lambda r:(r['traits']['soulBreak'],r['panel']['Speed'],r['score'],r['id']))
            breaker_roles[wave]=primary['id'];utilities[primary['id']]=['break']
        labels={'blockBuff':'禁止强化','antiHeal':'禁止恢复','strip':'驱散增益'}
        reasons[wave]['counters']={}
        for flag in sorted(counter_needs[index]):
            options=[r for r in group if r['traits'][flag]]
            reasons[wave]['counters'][flag]=[item_name(r['sid']) for r in options]
            if options:
                chosen=max(options,key=lambda r:(r['id']==breaker_roles.get(wave),r['panel']['Speed'],r['score'],r['id']))
                utilities.setdefault(chosen['id'],[]).append(flag)
            else:warnings.append(f'{"上半" if index==0 else "下半"}缺少符合关卡限制的{labels[flag]}角色')
        if not reasons[wave]['breakers']:warnings.append(f'{"上半" if index==0 else "下半"}没有符合关卡限制的可用破甲角色')
    if previous and set(output['upper']+output['lower'])==previous:warnings.append('当前合法满级角色有限，本轮沿用部分角色并重新检查队伍与配装')
    return {'waves':output,'reasons':reasons,'warnings':warnings,'ruleVersion':catalog()['version'],
            'breakerRoles':breaker_roles,'utilityRoles':utilities,'offensive':offensive}
