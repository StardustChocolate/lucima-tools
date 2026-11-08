"""三人核心召回、条件解释与六人联合约束；不模拟整场胜负。"""
from __future__ import annotations

import copy
import itertools

from ..game_client import GameError
from ..abyss_rules import conquer
from .catalog import catalog, digest, versions, TRAITS
from .adapters import HALVES

POOL_LIMIT = 60
HALF_LIMIT = 240
PAIR_LIMIT = 5


class Cancelled(GameError): pass


def output_index(role):
    panel = role['current']['panel']; scale = role['facts']['scale']
    divisor = {'HP': 15000, 'Defence': 1500, 'Attack': 3000}[scale]
    crit = max(0, min(1, panel['CriticalRate']/100)) if role['facts']['critical'] else 0
    return panel[scale]/divisor*(1+crit*(max(1, min(350, panel['CriticalDamageRate'])/100)-1))


def threat(enemies):
    chars = {row['id']: row for row in catalog()['characters']}
    tags = set().union(*(set(chars[e['staticID']]['tags']) for e in enemies))
    return {'tags': tags, 'elements': [chars[e['staticID']]['element'] for e in enemies],
            'unknownSpeed': sum(e['speed']['max'] is None for e in enemies),
            'speedMax': max((e['speed']['max'] for e in enemies if e['speed']['max'] is not None), default=None),
            'speedMin': max((e['speed']['min'] for e in enemies if e['speed']['min'] is not None), default=None)}


def _core(team, enemy):
    tags = set().union(*(set(r['tags']) for r in team)); wanted = {'break', 'protect'}
    if 'control' in enemy['tags'] or 'break' in enemy['tags']: wanted.add('cleanse')
    if 'revive' in enemy['tags']: wanted.add('antiRevive')
    if 'protect' in enemy['tags']: wanted.add('strip')
    coverage = len(wanted & tags)
    damage = max(output_index(r) for r in team)
    elements = sum(max(int(conquer({'Element':r['element']}, {'Element':e})) - int(conquer({'Element':e}, {'Element':r['element']}))
                       for r in team) for e in enemy['elements'])
    sustainability = sum('heal' in r['tags'] or 'protect' in r['tags'] or 'cleanse' in r['tags'] for r in team)
    fastest = max(r['current']['panel']['Speed'] for r in team)
    known_slow = enemy['speedMin'] is not None and fastest+.5 < enemy['speedMin']-.5
    route = int(not known_slow or bool({'heal','protect','cleanse'} & tags))
    speed_margin = int(not enemy['unknownSpeed'] and fastest > enemy['speedMax']+10)
    # 词典序：对局功能覆盖优先；属性指数仅在同层候选中比较，不当胜率。
    return (route, coverage, elements, min(sustainability, 2), speed_margin, damage,
            fastest, sum(r['current']['panel']['HP'] for r in team))


def _resources(team):
    gear = [eid for r in team for eid in r['equipIds']]
    arts = [r['artifact']['id'] for r in team if r.get('artifact')]
    if len(set(gear)) != 18 or len(set(arts)) != len(arts): return None
    if len({r['staticID'] for r in team}) != 3: return None
    return {'roles': {r['id'] for r in team}, 'static': {r['staticID'] for r in team}, 'gear': set(gear), 'artifacts': set(arts)}


def _retain(rows, limit):
    """保留多个输出、净化与保护核心，避免同一个共享角色占满候选。"""
    ordered = sorted(rows, key=lambda row: (tuple(-v for v in row[0]), row[1]))
    chosen = {}; buckets = {}
    for row in ordered:
        team = row[2]
        damage = max(team, key=lambda r: (output_index(r), r['id']))['id']
        for role in team:
            for tag in ('cleanse', 'protect', 'antiRevive', 'break'):
                if tag in role['tags']: buckets.setdefault((tag, role['id']), []).append(row)
        buckets.setdefault(('damage', damage), []).append(row)
    for bucket in buckets.values():
        for row in bucket[:3]: chosen[row[1]] = row
    # 若多样核心很多，再按统一规则排序截取；每半至少保留常规前列。
    merged = ordered[:limit//2]+list(chosen.values())+ordered
    seen = set(); result = []
    for row in merged:
        if row[1] in seen: continue
        seen.add(row[1]); result.append(row)
        if len(result) == limit: break
    return sorted(result, key=lambda row: (tuple(-v for v in row[0]), row[1]))


def check(check_id, status, reason, *, margin=None, required=False, dependencies=None):
    return {'id': check_id, 'status': status, 'reason': reason, 'margin': margin,
            'required': required, 'dependencies': dependencies or []}


def _matchup_key(row):
    checks = row[4]['checks']
    # Compatibility evidence, not a calibrated win probability.
    return (-sum(c['status']=='fail' and c['required'] for c in checks),
            -sum(c['status']=='fail' for c in checks),
            -sum(c['status']=='unknown' and c['required'] for c in checks), *row[0])


def assess(team, enemies, policy):
    enemy = threat(enemies); tags = set().union(*(set(r['tags']) for r in team)); checks = []
    speedy = max(team, key=lambda r: (r['current']['panel']['Speed'], r['id']))
    damage = max(team, key=lambda r: (output_index(r), r['id']))
    own_speed = speedy['current']['panel']['Speed']; high = enemy['speedMax']
    if enemy['unknownSpeed']:
        checks.append(check('panel-speed', 'unknown', f'敌方 {enemy["unknownSpeed"]} 名角色未填写速度，无法确认我方一速是否快超过 10 速，存在乱速风险。', required=True))
    elif own_speed > high+10:
        checks.append(check('panel-speed', 'pass', f'{speedy["name"]}为我方一速 {own_speed:g}，比敌方一速 {high:g} 快 {own_speed-high:g} 速，已超过 10 速余量。', margin=round(own_speed-high, 1)))
    elif own_speed > high:
        checks.append(check('panel-speed', 'unknown', f'乱速提醒：我方一速 {speedy["name"]} {own_speed:g}，仅比敌方一速 {high:g} 快 {own_speed-high:g} 速，未超过 10 速，仍可能被反超。', margin=round(own_speed-high, 1)))
    else:
        checks.append(check('panel-speed', 'fail', f'抢速提醒：我方一速 {own_speed:g} 未快于敌方一速 {high:g}，也未超过 10 速余量，需采用后手或反打路线。', margin=round(own_speed-high, 1)))
    checks.append(check('first-turn', 'unknown', '初始行动条、团队烙印/光环及开局触发未完整核验，面板更快不等于稳定先手。', required=True))
    checks.append(check('armor-break', 'pass' if 'break' in tags else 'unknown',
                        '带有破甲技能；是否命中及能否先于输出生效仍需核对。' if 'break' in tags else '当前三人未检索到破甲，击杀压力可能较大。'))
    if {'control', 'break'} & enemy['tags']:
        checks.append(check('cleanse', 'pass' if 'cleanse' in tags else 'unknown',
            '带有净化；需确认净化者能行动、覆盖目标与冷却。' if 'cleanse' in tags else '敌方可能施加控制/破甲，我方缺少已识别的队伍净化。', required=True))
    if 'revive' in enemy['tags']:
        checks.append(check('revival', 'pass' if 'antiRevive' in tags else 'unknown',
            '带有阻止复活被动，须保持来源角色存活且被动有效。' if 'antiRevive' in tags else
            '仅有击杀灭绝：需先完成击杀，不能当作无条件禁止复活。' if 'extinction' in tags else '敌方有复活能力，当前组合缺少已识别的反复活。', required=True))
    if 'protect' in enemy['tags']:
        checks.append(check('strip', 'pass' if 'strip' in tags else 'unknown',
            '带有驱散，仍需确认保护可驱散、命中与目标。' if 'strip' in tags else '敌方有保护机制，需确认能否驱散或绕过。'))
    if 'punishNonAttack' in enemy['tags']:
        checks.append(check('nonattack-response', 'unknown', '敌方会对非攻击技能产生响应；加攻、净化或拉条起手可能引起插队，不能默认连动成功。', required=True))
    if 'counter' in enemy['tags']:
        checks.append(check('counter', 'unknown', '敌方存在反击路径，需留意群攻、追加攻击后的反击与保护失效。'))
    if any(e['artifact']=='unknown' for e in enemies): checks.append(check('enemy-bonds', 'unknown', '部分敌方羁绊未知，护盾、分摊、抗控与拉条效果需补充。'))
    known_bonds = [e for e in enemies if e['artifact'] not in ('unknown', 'none')]
    if known_bonds:
        labels = {a['id']:a['name'] for a in catalog()['artifacts']}
        checks.append(check('known-enemy-bonds', 'unknown', '已记录敌方羁绊：'+
            '、'.join(labels[e['artifact']]+(f' Lv{e["artifactLevel"]}' if e['artifactLevel'] else '（等级未知）') for e in known_bonds)+
            '。触发、突破与叠加尚未完整模拟，不能忽略其反制。'))
    if any(r.get('artifact',{}).get('omittedEffects') for r in team if r.get('artifact')):
        checks.append(check('own-bond-triggers', 'unknown', '我方羁绊基础与常驻面板已计入；触发、光环和临场效果尚未模拟。'))
    resist = [e['stats']['ResistanceRate'] for e in enemies if e['stats']['ResistanceRate'] is not None]
    casters = [r for r in team if {'break','control','strip'} & set(r['tags'])]
    if resist and casters:
        hit = max(r['current']['panel']['EffectHitRate'] for r in casters); delta=round(hit-max(resist),1)
        checks.append(check('hit-resist-panel', 'unknown', f'我方相关角色最高命中 {hit:g}%，已知敌方最高抗性 {max(resist):g}%，面板差 {delta:+g} 个百分点。技能概率、免疫及条件无视抗性仍需核对。',margin=delta))
    hp_unknown = any(e['health']['kind']=='unknown' or e['health']['context']!='max' for e in enemies)
    checks.append(check('kill', 'unknown', ('最大生命资料不全；当前生命或百分比不能当成满血上限。' if hp_unknown else
        '已记录最大生命，但尚缺完整伤害、护盾与分摊结算。')+'本版本不判断必杀或完整战斗胜率。', required=True))
    for role in team:
        for issue in role['issues']: checks.append(check('mechanic-'+role['id'], 'unknown', role['name']+'：'+issue))
    if 'punishNonAttack' in enemy['tags']:
        strategy = '谨慎反打'
    elif 'cleanse' in tags and {'control', 'break'} & enemy['tags']:
        strategy = '净化反打'
    elif checks[0]['status']=='pass' and 'break' in tags:
        strategy = '抢速破甲（先手待核验）'
    elif 'protect' in tags or 'heal' in tags:
        strategy = '保护持续输出'
    else: strategy = '输出试配'
    order = sorted(team, key=lambda r: (-r['current']['panel']['Speed'], r['id']))
    steps = []
    for role in order:
        purpose = ('cleanse' if 'cleanse' in role['tags'] and {'control','break'} & enemy['tags'] else
                   'break' if 'break' in role['tags'] else 'strip' if 'strip' in role['tags'] and 'protect' in enemy['tags'] else
                   'protect' if role['id'] != damage['id'] and 'protect' in role['tags'] else None)
        skills = role['facts']['sources'].get(purpose, []) if purpose else []
        active = [s for s in role['skills'] if not s['passive'] and not s['hidden']]
        if not skills and active: skills = [active[-1]['id']]
        action = {'cleanse': '受控后优先净化；不要提前空放', 'break': '先尝试破甲关键目标，失败后保留防守路线',
                  'strip': '优先处理可驱散的保护', 'protect': '留意队友承伤，保护输出'} .get(purpose, '集中处理威胁目标；击杀条件尚未验证')
        if policy=='auto': action = '游戏 AI 自行决定技能与目标；此排序只展示面板快慢'
        steps.append({'roleId': role['id'], 'name': role['name'], 'speed': role['current']['panel']['Speed'],
                      'skillIds': skills, 'advice': action})
    if 'break' in tags:
        breaker = max((r for r in team if 'break' in r['tags']), key=lambda r:r['current']['panel']['Speed'])
        if breaker['id']!=damage['id'] and breaker['current']['panel']['Speed']<=damage['current']['panel']['Speed']:
            checks.append(check('break-order', 'unknown', '输出面板速度不低于破甲者，不能默认第一轮先破甲再输出。'))
    return {'strategy': strategy, 'coreId': damage['id'], 'checks': checks, 'steps': steps,
            'status': '需核对情报', 'policy': policy,
            'panelOrder': [r['id'] for r in order], 'orderNote': '仅按当前面板排列，实际行动会受技能、烙印、羁绊和初始行动条影响。'}


def recommend(roster, request, *, progress=lambda value: None, cancelled=lambda: False):
    def pulse(**value):
        if cancelled(): raise Cancelled('已停止 GVG 配队')
        progress(value)
    options = request['options']; by_id = {r['id']: r for r in roster['roles']}
    referenced = {*options['excluded'], *options['locks']['upper'], *options['locks']['lower']}
    if not referenced <= by_id.keys(): raise GameError('锁定或排除角色已不在当前账号，请重新选择')
    available = [r for r in roster['roles'] if r['ready'] and r['level']>=options['minLevel'] and r['id'] not in options['excluded']]
    selected_ids = {r['id'] for r in available}
    all_locked = {*options['locks']['upper'], *options['locks']['lower']}
    if not all_locked <= selected_ids: raise GameError('锁定角色未达到所选等级、装备不足或资源资料异常，请在我方角色中检查')
    locked_sids = [by_id[rid]['staticID'] for rid in all_locked]
    if len(set(locked_sids))!=len(locked_sids): raise GameError('上下半锁定了相同角色的不同实例，请只保留一个')
    base = {'schema': 1, 'request': request, 'inputHash': digest(request), 'snapshotHash': roster['snapshotHash'],
            'versions': versions(), 'plans': [], 'searchComplete': True, 'exhaustive': False,
            'notices': ['推荐只使用当前穿戴，不换装、不更换羁绊、不发送游戏战斗操作。',
                        '规则建议不是胜率；技能触发、承伤与击杀尚未完整模拟。'], 'diagnostics': {}}
    if len({r['staticID'] for r in available})<6:
        base['reason']='当前可用且穿齐六件装备的不同角色不足 6 名，请调整等级/排除或先完成配装。'
        return base
    halves = {}; examined = {}; pool_sizes = {}
    for half in HALVES:
        enemy = threat(request['enemies'][half]); other = 'lower' if half=='upper' else 'upper'
        reserved = {by_id[rid]['staticID'] for rid in options['locks'][other]}
        pool = [r for r in available if r['staticID'] not in reserved]
        # 小池完整枚举；大池保留锁定及按多种功能/属性取的候选，避免只有输出角色。
        if len(pool)>POOL_LIMIT:
            keep = {rid: by_id[rid] for rid in options['locks'][half]}
            for tag in TRAITS:
                rows = sorted((r for r in pool if tag in r['tags']), key=lambda r:(-r['current']['panel']['Speed'], -output_index(r), r['id']))
                for row in rows[:3]: keep[row['id']]=row
            ranked = sorted(pool, key=lambda r:(-_core([r],enemy)[0], -output_index(r), r['id']))
            for row in [*keep.values(), *ranked]:
                keep.setdefault(row['id'], row)
                if len(keep)>=POOL_LIMIT: break
            pool=list(keep.values())[:POOL_LIMIT]
        pool_sizes[half]=len(pool); locks = set(options['locks'][half]); rows=[]; count=0
        # 遍历固定锁定后的剩余位置，不枚举随后注定被锁定条件拒绝的组合。
        fixed = [by_id[rid] for rid in sorted(locks)]; rest = [r for r in pool if r['id'] not in locks]
        for extension in itertools.combinations(rest, 3-len(fixed)):
            count+=1
            if count%128==0: pulse(phase='halves', half=half, examined=count)
            team = tuple(sorted([*fixed, *extension], key=lambda r:r['id']))
            resources=_resources(team)
            if resources is None: continue
            rows.append((_core(team,enemy), tuple(r['id'] for r in team), team, resources))
        retained = _retain(rows, HALF_LIMIT)
        halves[half] = [(*row, assess(row[2], request['enemies'][half], options['policy'])) for row in retained]
        examined[half]=count
        pulse(phase='halves', half=half, examined=count)
    paired=[]; conflicts=0; compared=0
    quality = {half: {row[1]: _matchup_key(row) for row in halves[half]} for half in HALVES}
    for upper,lower in itertools.product(halves['upper'], halves['lower']):
        compared+=1
        if compared%512==0:pulse(phase='pairing', compared=compared)
        if any(upper[3][key]&lower[3][key] for key in ('roles','static','gear','artifacts')):
            conflicts+=1;continue
        uk,lk=quality['upper'][upper[1]],quality['lower'][lower[1]]
        weak,strong=sorted((uk,lk))
        # 默认先照顾较弱半场，用户可明确改成优先上半或下半。
        key = (*weak,*strong) if options['goal']=='balanced' else (*(uk if options['goal']=='upper' else lk), *weak,*strong)
        paired.append((key,upper,lower))
    paired.sort(key=lambda row:(tuple(-v for v in row[0]),row[1][1],row[2][1]))
    cores=set()
    for _,upper,lower in paired:
        # 上下半核心组合不同才占用一个推荐名额。
        ucore=max(upper[2],key=lambda r:(output_index(r),r['id']))['id'];lcore=max(lower[2],key=lambda r:(output_index(r),r['id']))['id']
        core=(ucore,lcore)
        if core in cores:continue
        cores.add(core); plan={'id':digest([upper[1],lower[1],options['policy']])[:20], 'halves': {}}
        for half,row in [('upper',upper),('lower',lower)]:
            detail=copy.deepcopy(row[4])
            detail['roles']=[copy.deepcopy(r) for r in sorted(row[2],key=lambda r:(-r['current']['panel']['Speed'],r['id']))]
            detail['resources']={key:sorted(val) for key,val in row[3].items()}
            plan['halves'][half]=detail
        plan.update(status='待核对战术建议', evidence='规则与当前面板', resourceCheck='六名角色、36 件装备及当前羁绊实例无重复', requiresChange=False,
                    rank=len(base['plans'])+1, rankingBasis='对局风险、关键机制覆盖、属性克制与当前面板；非实测胜率')
        base['plans'].append(plan)
        if len(base['plans'])>=PAIR_LIMIT:break
    pulse(phase='complete', compared=compared)
    base['diagnostics']={'availableRoles':len(available),'pools':pool_sizes,'coresExamined':examined,
                         'candidates':{key:len(value) for key,value in halves.items()},'pairsCompared':compared,'resourceConflicts':conflicts,
                         'candidateBudgetPerHalf':HALF_LIMIT,'evidence':'机制筛选与属性初筛；未开展战斗模拟'}
    if not base['plans']:base['reason']='本次搜索未找到六人及装备羁绊均不冲突的双队，请减少锁定或放开可用角色。'
    return base
