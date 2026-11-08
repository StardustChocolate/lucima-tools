"""One-team activity elites: two mob waves, boss party, healer and live friends."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy

from . import abyss_team
from .abyss_state import metadata, enabled
from .game_client import GameError
from .tasks import _oid, _build_support, item_name, SUPPORT_JOBS
from equipment import plans, recommend, stats


def active_healing(role):
    """Only visible, selectable heals count; hidden proc heals are not active."""
    tables=metadata()['tables'];definition=tables['Role'].get(role.get('StaticID'),{})
    levels={s.get('StaticID'):s for s in (role.get('Skills')or{}).get('Skills')or[] if isinstance(s,dict)}
    result={'heal':False,'group':False,'cleanse':False}
    for raw in str(definition.get('SkillIDs')or'').split('~'):
        sid,_,visibility=raw.partition(':');skill=tables['Skill'].get(sid,{})
        if not sid or visibility=='Hide' or skill.get('SkillType')=='Passive':continue
        for node in abyss_team.walk(abyss_team.skill_functions(role,levels.get(sid)or{'StaticID':sid,'Level':1})):
            if (node.get('TargetCamp')or skill.get('TargetCamp'))!='My' or node.get('Target') in ('Self','Source'):continue
            if node.get('Fun')=='AddHP':
                result['heal']=True
                result['group'] |= (node.get('Target')or skill.get('TargetType'))=='All'
            if node.get('Fun')=='RemoveDeBuf':result['cleanse']=True
    return result


def _candidate(role, state, *, rid=None, support=None):
    sid = str(role.get('StaticID') or '')
    if not enabled(metadata()['tables']['Role'].get(sid, {}).get('IsHero')) or int(role.get('LV') or 0)<=0 or not stats.role_base(role):
        return None
    rid = rid or _oid(role.get('_id'))
    if not rid:
        return None
    traits = abyss_team.role_traits(role)
    combat = abyss_team.combat_role(role, traits=traits)
    if combat['kind'] == 'healer' and not traits['heal']:
        return None
    if support is None:
        worn = plans.worn_items(state.get('EquipmentContainer', {}).get('Datas') or [], rid)[0]
        ranking = abyss_team.selection_state(state)
    else:
        worn = list((role.get('EquipmentMap') or {}).values())
        artifact = role.get('ArtifactData')
        ranking = {'Artifacts': {'Datas': [{**deepcopy(artifact), 'EquipRole': _oid(role.get('_id'))}]} if artifact else {}}
        # A friend's current items are read-only. An unknown bond must not
        # prevent using the rest of the verified support role.
        from equipment import bonds
        if artifact and not bonds.permanent_mods(artifact)[0]:
            ranking['Artifacts'] = {'Datas': []}
    panel = stats.role_panel(ranking, role, worn, include_artifact_effects=True)['panel']
    definition = metadata()['tables']['Role'][sid]
    profile = combat['profile']
    healer = combat['kind'] == 'healer'
    healing=active_healing(role) if healer else {'heal':False,'group':False,'cleanse':False}
    if healer:
        profile = {**profile, 'id': 'activity-healer', 'title': '治疗：生命＋防御＋抗性',
                   'priorities': ['HP', 'Defence', 'ResistanceRate'], 'critical': False,
                   'ranking': None, 'hardConditions': [], 'hitTarget': False, 'metricLabel': '治疗生存参考分'}
    return {'id': str(rid), 'sid': sid, 'job': definition.get('Job'), 'element': definition.get('Element'),
            'role': role, 'traits': traits, 'combat': combat, 'panel': panel,
            'output': recommend.metric(panel, combat['profile']), 'profile': profile,
            'healer': healer, 'damage': combat['kind'] in ('single', 'area'),
            'healing':healing,
            'breaker': traits['break'] or traits['soulBreak'],
            'quality': recommend.metric(panel, profile), 'level': int(role.get('LV') or 0),
            'training': abyss_team.training_preference(role), 'support': support}


def _key(group, support=None):
    return (tuple(sorted(row['sid'] for row in group)),
            str((support or {}).get('id') or ''))


def choose(state, scene, supports=(), *, excluded=(), usage=None, cancelled=None):
    """Bounded beam search over current owned heroes and real friend job slots."""
    usage = usage or Counter()
    excluded = set(excluded)
    waves = scene.get('Waves') or []
    final = waves[-1].get('Ms') or []
    bosses = [m for m in final if enabled(m.get('Boss'))] or final
    boss_stage=any(enabled(m.get('Boss')) for m in final)
    enemy, _ = abyss_team._enemy_traits({'Monsters': bosses})
    boss_elements = [metadata()['tables']['Role'][m['M']].get('Element') for m in bosses]
    party_elements = boss_elements
    own = []
    for role in state.get('RoleDataContainer', {}).get('Roles') or []:
        if cancelled and cancelled():
            raise GameError('活动自动推图已停止')
        if int(role.get('LV') or 0) <= 0:
            continue
        row = _candidate(role, state)
        if row:
            own.append(row)
    # Multiple combat instances of one hero cannot occupy two slots.
    distinct = {}
    for row in sorted(own, key=lambda r:(r['quality'],r['training'],r['id']), reverse=True):
        distinct.setdefault(row['sid'], row)
    own = list(distinct.values())
    friends = []
    for entry in supports:
        if not isinstance(entry, dict) or not entry.get('IsFriend'):
            continue
        cuid = entry.get('PlayerInfo', {}).get('CUID')
        if cuid is None:
            continue
        for member in entry.get('RoleDataList') or []:
            job = member.get('SupportJob')
            if job not in SUPPORT_JOBS:
                continue
            role = member.get('Role') or {}
            row = _candidate(role, state, rid='support:' + str(cuid) + ':' + job, support=_build_support(entry,job))
            if row:
                friends.append(row)
    if not any(row['healer'] and row['healing']['heal'] for row in own):
        raise GameError('第13关自动配队需要一名有主动治疗技能的自有奶妈，请培养后重试')
    if not any(row['breaker'] for row in own):
        raise GameError('第13关自动配队需要一名自有破甲角色首发，请培养破甲角色后重试')

    def match(row, elements):
        if row['sid'] in abyss_team.PREFERRED_DAMAGE_HEROES or row['traits']['forceConquer']:
            return 0
        return sum(abyss_team.element_matchup(row['element'], element) for element in elements)

    def individual(row):
        return (row['level']==60,row['sid'] in abyss_team.PREFERRED_DAMAGE_HEROES and row['level']==60, row['damage'], match(row,boss_elements),
                -abyss_team._output_tier(row['combat']), row['quality'],row['training'],row['id'])

    # Passive/hidden healing cannot occupy the required active healer slot.
    own=[row for row in own if not row['healer'] or row['healing']['heal']]
    def healer_fit(row):
        if not row['healer']:return (False,False,0,0)
        return (bool(enemy['debuffs']) and row['healing']['cleanse'],row['healing']['group'],
                -sum(abyss_team.element_matchup(element,row['element']) for element in boss_elements),row['quality'])
    for row in own+friends:
        row['bossFit']=match(row,boss_elements);row['partyFit']=match(row,party_elements)

    pool = sorted(own, key=individual, reverse=True)[:20]
    pool += sorted((row for row in own if row['healer']),key=lambda r:(r['level']==60,healer_fit(r),r['training']),reverse=True)[:6]
    for flag in ('break','strip','antiHeal','blockBuff','cleanse'):
        pool += sorted((row for row in own if row['traits'][flag]),key=individual,reverse=True)[:4]
    pool += sorted((row for row in own if row['combat']['kind']=='area'),key=individual,reverse=True)[:6]
    pool = list({r['id']: r for r in pool}.values())
    # The reserve may be the same static hero as an owned starter. Keep each
    # friend's best job slot so another friend's H165 remains an option.
    support_pool = {}
    for row in sorted(friends,key=lambda r:(r['level'],r['quality'],r['training']),reverse=True):
        support_pool.setdefault((row['support']['PlayerRoleData']['PlayerInfo']['CUID'],row['sid']),row)
    support_options = sorted(support_pool.values(),key=lambda r:(r['sid']=='H165',r['damage'],match(r,boss_elements),r['quality']),reverse=True)[:16]
    support_options.append(None)

    def score(group):
        ids={row['sid'] for row in group}
        pair='H119' in ids and 'H165' in ids
        pair_tried=usage.get('H119',0)>0 and usage.get('H165',0)>0
        damage = [row for row in group if row['damage']]
        needs = [('break',True,'ReduceDef'), ('antiHeal',enemy['recovery'],'Unhealable'),
                 ('blockBuff',enemy['protection'],'CannotBuff'), ('strip',bool(enemy['buffs'] or enemy['protection']),None)]
        counters = sum(needed and (immune is None or immune not in enemy['immune'])
                       and any(row['traits'][flag] for row in group) for flag,needed,immune in needs)
        return (pair and not pair_tried and 'SealPassive' not in enemy['debuffs'],
                any(row['sid']=='H165' and row['level']==60 for row in group) and not usage.get('H165',0),
                -int(pair and pair_tried),
                int(any(row['healer'] for row in group)),
                int(any(row['breaker'] for row in group)),
                sum(r['sid'] in abyss_team.PREFERRED_DAMAGE_HEROES and r['level']==60 for r in damage),
                sum(row['level']==60 for row in group),
                max((healer_fit(row) for row in group if row['healer']),default=(False,False,0,0)),
                counters, int(any(row['combat']['kind']=='single' if boss_stage else row['combat']['kind']=='area' for row in damage)),
                int(any(row['traits']['cleanse'] for row in group) and bool(enemy['debuffs'])),
                -sum(usage.get(row['sid'],0) for row in group),
                sum(row['bossFit'] for row in damage),sum(row['partyFit'] for row in damage),
                sum(row['damage'] for row in group),sum(row['quality'] for row in group),
                tuple(sorted(row['id'] for row in group)))

    # Plan four owned starters once, independent of the reserve. A friend's
    # healing, armor break or damage cannot satisfy the initial team needs.
    beam = [((),-1)]
    for size in range(4):
        expanded = []
        for group,last in beam:
            for index in range(last+1,len(pool)):
                if cancelled and cancelled():
                    raise GameError('活动自动配队已停止')
                next_group = (*group,pool[index])
                healers = sum(row['healer'] for row in next_group)
                if healers>1 or (size==3 and (healers!=1 or not any(row['breaker'] for row in next_group)
                                             or sum(row['damage'] for row in next_group)<2)):
                    continue
                expanded.append((next_group,index))
        beam = sorted(expanded,key=lambda pair:score(pair[0]),reverse=True)[:96]
    best = None
    for group,_ in beam:
        for friend in support_options:
            if _key(group,friend) in excluded:
                continue
            reserve = ((friend['sid']=='H165',True,friend['level']==60,
                        -usage.get('support:'+friend['sid'],0),friend['damage'],friend['bossFit'],friend['quality'])
                       if friend else (False,False,False,0,False,0,0))
            value = (score(group),reserve)
            if best is None or value>best[0]:
                best = (value,group,friend)
    if best is None:
        raise GameError('没有更多含治疗位且不重复的可用阵容，请前往游戏手动挑战第13关')
    group = abyss_team._arrange_group(best[1],protect_back=any(row['combat']['protectsBack'] for row in best[1]))
    big_brother=next((row for row in group if row['sid']=='H165'),None)
    if big_brother:
        group.remove(big_brother);group.append(big_brother)
    local = list(group)
    friend = best[2]
    utilities = {}
    for flag in ('break','strip','antiHeal','blockBuff'):
        if flag=='antiHeal' and not enemy['recovery']:continue
        if flag=='blockBuff' and not enemy['protection']:continue
        if flag=='strip' and not (enemy['buffs'] or enemy['protection']):continue
        options = [row for row in local if not row['healer'] and (row['breaker'] if flag=='break' else row['traits'][flag])]
        if options:
            chosen = max(options,key=lambda row:(row['panel']['Speed'],row['quality']))
            utilities.setdefault(chosen['id'],[]).append(flag)
    return {'key':_key(group,friend),'roles':[row['id'] for row in local],
            'positions':{row['id']:pos for pos,row in enumerate(group)},
            'support':deepcopy(friend['support']) if friend else None,
            'supportName':item_name(friend['sid']) if friend else '',
            'staticIds':[row['sid'] for row in local], 'supportStaticId':friend['sid'] if friend else '',
            'healerNames':[item_name(row['sid']) for row in group if row['healer']],
            'profileOverrides':{row['id']:row['profile'] for row in local if row['healer']},
            'utilityRoles':utilities,'names':[item_name(row['sid']) for row in group]}
