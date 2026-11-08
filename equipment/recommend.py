"""按角色的属性方向推荐。纯快照计算，不发送任何游戏操作。

小库存完整枚举；大库存按方向保留装备、束搜索并明确报告未穷尽。
与旧按条件模式分离，同一显示速度下可以保留多种真实装备组合。
羁绊锁定为上方已穿戴的实物，所有面板复用 stats，不推定临场效果。
"""
from __future__ import annotations

import copy
import heapq
import itertools
import json
import math
from functools import lru_cache
from pathlib import Path

from backend.abyss_state import metadata
from . import auto, plans, stats, tactics

VERSION = '20261025.3'
EXACT_LIMIT = 30_000
RESULT_LIMIT = 60
BUDGETS = {'standard': (128, 40), 'deep': (320, 64)}
SCALES = {'Speed': 200, 'HP': 15000, 'Defence': 1500, 'Attack': 3000,
          'EffectHitRate': 150, 'ResistanceRate': 150, 'CriticalRate': 100, 'CriticalDamageRate': 300}
WEIGHTS = (.50, .35, .15)


@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name('recommend_profiles.json').read_text(encoding='utf-8'))


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


@lru_cache(maxsize=300)
def _profiles(sid):
    curated = catalog()['roles'].get(sid)
    if curated:
        return curated
    tables = metadata()['tables']
    row = tables['Role'].get(sid, {})
    skills = [tables['Skill'].get(key.split(':')[0], {}) for key in row.get('SkillIDs', '').split('~')]
    nodes = [node for skill in skills for node in _walk(skill.get('Dynamic', {}))]
    # 只把当前被动直接声明的 NoCritical 当角色级效果，主动技能不扩大适用范围。
    no_crit = any(node.get('name') == 'NoCritical' and node.get('value')
                  for skill in skills if skill.get('SkillType') == 'Passive' for node in _walk(skill.get('Dynamic', {})))
    scaling = [node['ConstAttack'].get('Property') for node in nodes if isinstance(node.get('ConstAttack'), dict)]
    damage_stat = 'Defence' if 'Defence' in scaling else 'HP' if 'MaxHP' in scaling else 'Attack'
    control = any(node.get('Fun') == 'AddStatus' and node.get('TargetCamp', 'Enemy') != 'My' and node.get('Target') != 'Self' for node in nodes)
    support = row.get('Job') == 'Medic' or any(node.get('Fun') in ('AddHP', 'Resurrection', 'Revive', 'RemoveDeBuf')
        and node.get('TargetCamp') == 'My' and node.get('Target', 'Self') != 'Self' for node in nodes)
    result = []
    def add(pid, title, note, priorities, critical=False):
        result.append(dict(id=pid, title=title, description=note, priorities=priorities,
                           damageStat=damage_stat, critical=critical, source='原始技能结构与通用属性方向；非专属战术档案'))
    if control:
        add('control', '速度＋命中', '技能包含敌方状态效果，比较速度与状态命中；不预判控制成功率。', ['Speed', 'EffectHitRate', 'HP'])
    if not support:
        name = {'HP': '生命', 'Defence': '防御', 'Attack': '攻击'}[damage_stat]
        add('output', f'{name}输出', f'按{name}与有效暴击属性作输出参考，不是技能实战伤害。', ['damage', 'Speed', damage_stat], not no_crit)
    add('survive', '速度＋生命', '兼顾行动与存活，可与其他方向对比选装。', ['Speed', 'HP', 'Defence'])
    add('resist', '速度＋抗性', '兼顾行动、抗控与生存，不设固定毕业门槛。', ['Speed', 'ResistanceRate', 'HP'])
    return result[:4]


def profiles(role):
    result = copy.deepcopy(_profiles(str(role.get('StaticID') or '')))
    audit = tactics.analyze(role)
    added = tactics.profiles(role, result[0]['damageStat'])
    existing = {profile['id'] for profile in result}
    result.extend(profile for profile in added if profile['id'] not in existing)
    for profile in result:
        profile.update(version=VERSION, dedicated=str(role.get('StaticID') or '') in catalog()['roles'],
                       metricLabel={'speed-first': '速度优先', 'hit-first': '命中优先'}.get(profile.get('ranking'), '方向参考分'), scope='个人面板属性；不含临场触发、站位收益与对局模拟')
        if profile['id'] == 'speed-crit' and not profile.get('evidence'):
            profile['evidence'] = [e for e in audit['effects'] if not e['soulOnly'] and e['kind'] == 'pull']
        if audit['speedPenalty']:
            profile['caution'] = '此角色被动降低速度收益并依靠队友触发拉条，不宜直接套用抢第一速配装。'
    return result


def metric(panel, profile):
    """有固定归一化尺度的属性方向分；不叫伤害、胜率或毕业达成度。"""
    base = panel.get(profile['damageStat'], 0)
    # 真实暴击概率，0 暴击不等于必暴；禁止暴击的用途不读取暴击/暴伤。
    critical = max(0, min(1, panel.get('CriticalRate', 0) / 100)) if profile['critical'] else 0
    multiplier = 1 + critical * (max(1, min(350, panel.get('CriticalDamageRate', 150)) / 100) - 1)
    damage = base * multiplier / SCALES[profile['damageStat']]
    values = [damage if key == 'damage' else panel.get(key, 0) / SCALES[key] for key in profile['priorities']]
    return sum(weight * value for weight, value in zip(WEIGHTS, values)) * 100


def rank_key(panel,profile):
    """用途明确区分抢速和高命中，次级属性不能抵消一级属性差。"""
    ranking = profile.get('ranking')
    if ranking in ('speed-first', 'hit-first'):
        first = 'Speed' if ranking == 'speed-first' else 'EffectHitRate'
        return (panel[first], *(metric(panel, {**profile, 'priorities': ['damage']}) if stat == 'damage' else panel[stat]
                               for stat in profile.get('tieBreak', ['damage', 'HP'])))
    return (metric(panel,profile),)


def profile_conditions(user_conditions,profile,hit_target=100):
    merged={row['stat']:dict(row) for row in user_conditions}
    rules = [*(profile.get('hardConditions') or []), *([{'stat': 'EffectHitRate', 'min': hit_target}] if profile.get('hitTarget') else [])]
    for rule in auto.normalize_conditions(rules):
        row=merged.setdefault(rule['stat'],{'stat':rule['stat'],'min':None,'max':None})
        for bound,compare in (('min',max),('max',min)):
            if rule[bound] is not None:row[bound]=rule[bound] if row[bound] is None else compare(row[bound],rule[bound])
    return list(merged.values())


def rule_note(profile, constraints):
    required = [f"{stats.STAT_CN[row['stat']]}至少 {row['min']:g}%" for row in constraints
                if row['stat'] in ('CriticalRate', 'EffectHitRate') and row['min'] is not None and row['min'] > 0]
    first = '先满足' + '、'.join(required) + '；' if required else ''
    if profile.get('ranking') == 'hit-first':
        return first + '按状态命中从高到低，同命中再比较速度、生存。'
    if profile.get('ranking') == 'speed-first':
        tie = '、'.join('输出' if key == 'damage' else stats.STAT_CN[key] for key in profile.get('tieBreak', ['damage', 'HP']))
        return first + f'按速度从高到低，同速比较{tie}。'
    return first + '按该用途的属性综合评分排序。'


def _panel(fixed, vector, base, counts):
    bonus, _ = stats.set_mods(counts)
    additions = stats.contribution(base, bonus)
    return stats.display({stat: fixed[stat] + vector[i] + additions[stat] for i, stat in enumerate(stats.STATS)})


def search(state, role, *, profile_id=None, depth='standard', conditions=None, sets=None, set_options=None, hit_target=100,
           only_max=True, include_worn=False, free_roles=None, exclude=None, excluded_roles=None,
           progress=None, cancelled=None, profile_override=None):
    if depth not in BUDGETS:
        raise auto.BuildError('请选择标准推荐或深度推荐')
    if isinstance(hit_target, bool) or not isinstance(hit_target, (int, float)) or not math.isfinite(hit_target) or not 0 <= hit_target <= 500:
        raise auto.BuildError('最低状态命中须为 0～500%')
    # Internal Abyss tactics use the same panel/search implementation with one
    # explicit purpose. Public equipment requests still use the role catalog.
    options = [copy.deepcopy(profile_override)] if profile_override is not None else profiles(role)
    if profile_id is not None and profile_id not in {p['id'] for p in options}:
        raise auto.BuildError('该角色的推荐方向已变化，请重新选择')
    chosen = profile_id or options[0]['id']
    options.sort(key=lambda p: p['id'] != chosen)
    need = auto.normalize_sets(sets)
    groups=auto.selected_set_groups(set_options) if set_options is not None else [need]
    conds = auto.normalize_conditions(conditions)
    constraints={profile['id']:profile_conditions(conds,profile,hit_target) for profile in options}
    for profile in options:
        profile['ruleNote'] = rule_note(profile, constraints[profile['id']])
    base = stats.role_base(role)
    if not base:
        raise auto.BuildError('缺少角色基础属性资料')
    modifiers = stats.role_mods(state, role, include_artifact_effects=True)
    fixed = stats.panel(base, modifiers)
    set_base = {key: value * (1 + modifiers.get(key + 'FinalRate', 0)) for key, value in base.items()}
    pool = auto.candidate_pool(state, role, only_max=only_max, include_worn=include_worn,
                               free_roles=free_roles, exclude=exclude,
                               excluded_roles=excluded_roles if include_worn else [])
    by_slot = {slot: [] for slot in plans.SLOTS}
    for item in sorted(pool, key=lambda item: plans._oid(item['_id'])):
        contrib = stats.contribution(base, stats.equip_mods(item), modifiers)
        by_slot[plans.equip_slot(item)].append(dict(id=plans._oid(item['_id']), raw=item, set=item.get('Set') or '', aux=auto._aux_score((item.get('SubProps') or {}).get('SourceValues') or []),
                                                     vec=tuple(contrib[key] for key in stats.STATS)))
    pool_counts = {slot: len(rows) for slot, rows in by_slot.items()}
    count = math.prod(pool_counts.values())
    exact = count <= EXACT_LIMIT
    width, item_limit = BUDGETS[depth]
    heaps = {p['id']: [] for p in options}
    seen = {p['id']: set() for p in options}
    nodes = 0
    evaluated = 0
    prefiltered = False
    zero = (0.,) * len(stats.STATS)
    role_id = plans._oid(role['_id'])
    worn, _ = plans.worn_items((state.get('EquipmentContainer') or {}).get('Datas') or [], role_id)
    worn_ids = {plans._oid(item['_id']) for item in worn}
    serial = itertools.count()
    remaining_hi = [tuple(sum(max((item['vec'][j] for item in by_slot[slot]), default=0) for slot in plans.SLOTS[i:]) for j in range(len(stats.STATS))) for i in range(7)]
    remaining_lo = [tuple(sum(min((item['vec'][j] for item in by_slot[slot]), default=0) for slot in plans.SLOTS[i:]) for j in range(len(stats.STATS))) for i in range(7)]
    available_sets = {item['set'] for items in by_slot.values() for item in items}
    set_vectors = [(name, info['count'], stats.contribution(set_base, info['rates']))
                   for name, info in stats.tables()['sets'].items() if name in available_sets]
    set_limits = {stat: [(name, pieces, vector[stat]) for name, pieces, vector in set_vectors if vector[stat]] for stat in stats.STATS}

    def set_bound(stat, counts, remaining):
        low = high = 0.
        for name, pieces, value in set_limits[stat]:
            known = counts.get(name, 0)
            a, b = (known//pieces)*value, ((known+remaining)//pieces)*value
            low += min(a,b)
            high += max(a,b)
        return low, high

    def trim_beam(expansions, profile):
        ordered = heapq.nlargest(width, expansions, key=lambda e: (e[0], e[1]))
        minimums = [c for c in constraints[profile['id']] if c['min'] is not None and c['min'] > 0]
        if width < 2 or not minimums or len(expansions) <= width:
            return [entry[2] for entry in ordered]
        # Keep promising high-resource partial outfits as well as the fastest
        # partial outfits. Otherwise early high-speed/zero-hit states can crowd
        # out a slightly slower item needed to reach the final hit/crit floor.
        representatives = {}
        for entry in expansions:
            bucket = tuple(int(min(c['min'], entry[3][c['stat']]) / max(c['min']/20, .1)) for c in minimums)
            old = representatives.get(bucket)
            if old is None or (entry[0], entry[1]) > (old[0], old[1]):
                representatives[bucket] = entry
        diverse = sorted(representatives.values(), key=lambda e: (e[0], e[1]), reverse=True)
        kept, used = [], set()
        for entry in [*ordered[:width//2], *diverse, *ordered]:
            if entry[1] not in used:
                used.add(entry[1]);kept.append(entry[2])
            if len(kept) == width:break
        return kept

    def check(profile=None):
        if cancelled and cancelled():
            raise auto.SearchCancelled('已停止推荐筛选')
        if progress:
            progress(dict(mode='advisor', nodes=nodes, evaluated=evaluated,
                          profileName=profile['title'] if profile else '全部方向',
                          profileCount=len(options), speedCount=sum(len(h) for h in heaps.values())))
        if cancelled and cancelled():
            raise auto.SearchCancelled('已停止推荐筛选')

    def offer(picked, vector, counts, active):
        nonlocal evaluated
        evaluated += 1
        if not any(all(counts.get(name,0)>=pieces for name,pieces in group.items()) for group in groups):
            return
        shown = _panel(fixed, vector, set_base, counts)
        if any((c['min'] is not None and shown[c['stat']] < c['min']) or
               (c['max'] is not None and shown[c['stat']] > c['max']) for c in conds):
            return
        identity = tuple(item['id'] for item in picked)
        borrowed = sum(bool(plans._oid(item['raw'].get('EquipRole'))) and plans._oid(item['raw'].get('EquipRole')) != role_id for item in picked)
        changes = sum(item['id'] not in worn_ids for item in picked)
        for profile in active:
            pid = profile['id']
            if any((c['min'] is not None and shown[c['stat']]<c['min']) or (c['max'] is not None and shown[c['stat']]>c['max']) for c in constraints[pid]):
                continue
            if identity in seen[pid]:
                continue
            value = metric(shown, profile)
            key = (*rank_key(shown,profile), -borrowed, -changes)
            heap = heaps[pid]
            if len(heap) >= RESULT_LIMIT and key < heap[0][0]:
                continue
            combo = dict(equipIds=dict(zip(plans.SLOTS, identity)), panel=shown,
                         sets=stats.set_mods(counts)[1], aux=sum(item['aux'] for item in picked),
                         metrics=auto.metrics(shown, 100), advisorScore=shown['Speed'] if profile.get('ranking')=='speed-first' else shown['EffectHitRate'] if profile.get('ranking')=='hit-first' else round(value, 2), profileId=pid,
                         metricLabel=profile['metricLabel'],ranking=profile.get('ranking'), ruleNote=profile['ruleNote'],
                         borrowed=borrowed, changes=changes)
            value_tuple = (key, next(serial), identity, combo)
            if len(heap) >= RESULT_LIMIT:
                removed = heapq.heapreplace(heap, value_tuple)
                seen[pid].discard(removed[2])
            else:
                heapq.heappush(heap, value_tuple)
            seen[pid].add(identity)

    def visit(index, picked, vector, counts):
        nonlocal nodes
        nodes += 1
        if nodes % 256 == 0:
            check()
        if index == 6:
            offer(picked, vector, counts, options)
            return
        for item in by_slot[plans.SLOTS[index]]:
            next_counts = dict(counts)
            next_counts[item['set']] = next_counts.get(item['set'], 0) + 1
            visit(index + 1, (*picked, item), tuple(x+y for x, y in zip(vector, item['vec'])), next_counts)

    check()
    if count and exact:
        visit(0, (), zero, {})
    elif count:
        for profile in options:
            check(profile)
            if any(c['min'] is not None and c['max'] is not None and c['min']>c['max'] for c in constraints[profile['id']]):continue
            bounds = [(c, stats.STATS.index(c['stat'])) for c in constraints[profile['id']]]
            beam = [((), zero, {})]
            for slot in plans.SLOTS:
                # 记录启发式预选，不声称完整搜索。保留当前穿戴、各套装和方向属性替代。
                items = by_slot[slot]
                score_item = lambda item: rank_key(_panel(fixed, item['vec'], set_base, {item['set']: 1}), profile)
                ranked = sorted(items, key=lambda item: (tuple(-v for v in score_item(item)), item['id']))
                if len(ranked) > item_limit:
                    prefiltered = True
                    diverse = [item for item in ranked if item['id'] in worn_ids]
                    for set_name in sorted({item['set'] for item in ranked}):
                        diverse += [item for item in ranked if item['set'] == set_name][:2]
                    for key in dict.fromkeys([*profile['priorities'],*(c['stat'] for c in constraints[profile['id']] if c['min'] is not None)]):
                        if key in stats.STATS:
                            diverse += sorted(ranked, key=lambda item: (-item['vec'][stats.STATS.index(key)], item['id']))[:4]
                    diverse += ranked
                    priority = list(dict.fromkeys(item['id'] for item in diverse))[:item_limit]
                    mapping = {item['id']: item for item in ranked}
                    ranked = [mapping[aid] for aid in priority]
                expansions = []
                for picked, vector, counts in beam:
                    for item in ranked:
                        nodes += 1
                        if nodes % 256 == 0:
                            check(profile)
                        next_counts = dict(counts)
                        next_counts[item['set']] = next_counts.get(item['set'], 0) + 1
                        # 无法满足用户套装要求的分支可以安全丢弃。
                        remaining = 5 - len(picked)
                        if not any(sum(max(0,pieces-next_counts.get(name,0)) for name,pieces in group.items())<=remaining for group in groups):
                            continue
                        next_vector = tuple(x+y for x, y in zip(vector, item['vec']))
                        possible = True
                        for condition, stat_index in bounds:
                            stat = condition['stat']
                            set_low, set_high = set_bound(stat, next_counts, remaining)
                            upper = fixed[stat]+next_vector[stat_index]+remaining_hi[len(picked)+1][stat_index]+set_high
                            lower = fixed[stat]+next_vector[stat_index]+remaining_lo[len(picked)+1][stat_index]+set_low
                            if (condition['min'] is not None and stats.display({stat:upper})[stat]<condition['min']) or (condition['max'] is not None and stats.display({stat:lower})[stat]>condition['max']):
                                possible = False
                                break
                        if not possible:continue
                        group = ((*picked, item), next_vector, next_counts)
                        if len(picked) == 5:
                            offer(*group, [profile])
                        else:
                            partial = _panel(fixed, next_vector, set_base, next_counts)
                            value = rank_key(partial, profile)
                            expansions.append((value, tuple(it['id'] for it in group[0]), group, partial))
                beam = trim_beam(expansions, profile)
        # 当前可用且完整的穿戴始终作为基线候选，不受启发式预选影响。
        if len(worn_ids) == 6:
            selected = [next((item for item in by_slot[slot] if item['id'] in worn_ids), None) for slot in plans.SLOTS]
            if all(selected):
                counts = {}
                vector = list(zero)
                for item in selected:
                    counts[item['set']] = counts.get(item['set'], 0) + 1
                    vector = [x+y for x, y in zip(vector, item['vec'])]
                offer(selected, vector, counts, options)
    results = []
    gear_map = {item['id']: item['raw'] for slot in by_slot.values() for item in slot}
    for profile in options:
        rows = sorted(heaps[profile['id']], key=lambda row: (tuple(-v for v in row[0]), row[2]))
        for _, _, _, combo in rows:
            # 交付的六件装备重新走共用完整面板，防止增量与最终展示不一致。
            complete = [gear_map[aid] for aid in combo['equipIds'].values()]
            if stats.role_panel(state, role, complete, include_artifact_effects=True)['panel'] != combo['panel']:
                raise auto.BuildError('推荐面板核对失败，请重新同步账号')
            results.append(combo)
    check()
    profile_counts={profile['id']:len(heaps[profile['id']]) for profile in options}
    unavailable_reason=None
    # 主用途不达标时不偷选另一种用途，也不为后续角色预占不符合主用途的装备。
    if not profile_counts[chosen]:
        results=[]
        selected=next(profile for profile in options if profile['id']==chosen)
        if selected.get('hardConditions') or selected.get('hitTarget'):
            conflict=any(c['min'] is not None and c['max'] is not None and c['min']>c['max'] for c in constraints[chosen])
            needed = '、'.join(f"{stats.STAT_CN[c['stat']]}至少 {c['min']:g}%" for c in constraints[chosen] if c['stat'] in ('CriticalRate', 'EffectHitRate') and c['min'] is not None)
            unavailable_reason=(f'该用途要求{needed}，与额外属性范围冲突，请调整限制或切换用途。' if conflict else
                                f'该用途要求{needed}，当前推荐未找到满足要求的套装，请调整门槛、尝试深度推荐或切换用途。')
    return dict(results=results, profiles=options, profileCounts=profile_counts, advisor=True, searchComplete=True, exhaustive=exact,
                unavailableReason=unavailable_reason,
                pool=pool_counts, bestPossible={}, shortage={},
                current=stats.role_panel(state, role, worn, include_artifact_effects=True),
                report=dict(version=VERSION, exhaustive=exact, prefilterApplied=prefiltered, combinations=count,
                            evaluated=evaluated, nodes=nodes, depth=depth, limitPerProfile=RESULT_LIMIT),
                artifactId=next((plans._oid(a['_id']) for a in (state.get('Artifacts') or {}).get('Datas') or [] if plans._oid(a.get('EquipRole')) == role_id), None))
