"""Explain equipment uses from the current role's actual skill mechanics.

Only selectable skills and their reachable references supply an opener use.
Self/ally effects never imply effectiveness; reactive passives are not free casts.
This module describes capabilities, not a prediction of turn order or hit chance.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache

from backend.abyss_state import direct_properties, metadata
from backend.abyss_properties import _skill_setting, combine_functions
from backend.game_client import GameError


LABELS = {
    'pull': '队友拉条', 'haste': '队友提速', 'push': '敌方推条',
    'slow': '降速', 'silence': '沉默', 'seal': '被动封锁',
    'suppress': '技能压制', 'cooldown': '增加技能冷却', 'stun': '眩晕',
    'sleep': '睡眠', 'taunt': '嘲讽', 'restrict': '拘禁', 'strip': '驱散增益', 'refresh': '队友减少冷却',
}
CONTROL = {'ReduceSpeed': 'slow', 'Silence': 'silence', 'Seal': 'seal',
           'Suppress': 'suppress', 'Stun': 'stun', 'Sleep': 'sleep',
           'Taunt': 'taunt', 'Restrict': 'restrict'}
REFERENCES = {'AdditionSkill', 'CounterSkill', 'AddPinchSkill', 'ReplaceSkill'}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _number(value, default=0):
    return float(value) if isinstance(value, (float, int)) else default


def _sign(node):
    values = [_number(node.get('Value'))]
    converter = node.get('ValueConvert') or {}
    values += [_number(converter.get(key)) for key in ('Base', 'Rate')]
    return min(values) if any(v < 0 for v in values) else max(values)


def _conditions(node):
    return [row for row in (node.get('Condition'), node.get('PreCond'), *(node.get('Conditions') or [])) if isinstance(row, dict)]


def _target(node, definition):
    selection = node.get('SelectInfo') or {}
    target = selection.get('TargetType') or node.get('Target') or definition.get('TargetType') or 'SkillSelect'
    camp = selection.get('TargetCamp') or node.get('TargetCamp') or ('My' if target == 'Self' else 'Enemy')
    return camp, target


def _status_kind(sid, tables):
    row = tables['Status'].get(sid, {})
    kind = CONTROL.get(row.get('Group') or sid)
    if kind:
        return kind
    props = direct_properties(row.get('Dynamic', {}))
    if _number(props.get('SpeedFinalRate')) < 0:
        return 'slow'
    for key, category in (('Silence', 'silence'), ('SealPassive', 'seal'), ('Dizzy', 'stun'), ('Sleep', 'sleep')):
        if props.get(key):
            return category
    return None


def _soul_functions(role, record):
    # Reuse the version-checked original soul-function catalog; absent data does
    # not invent a resistance bypass. Normal recommendation still works.
    try:
        from backend.abyss_team import skill_functions
        return skill_functions(role, record, soul=True)
    except GameError:
        return []


def _analyze(role):
    tables = metadata()['tables']
    sid = role['StaticID']
    row = tables['Role'].get(sid, {})
    records = {r['StaticID']: r for r in (role.get('Skills') or {}).get('Skills') or [] if r.get('StaticID')}
    effects, passive_props = [], {}
    roots = [(key.split(':')[0], ':Hide' in key) for key in row.get('SkillIDs', '').split('~') if key]

    def visit(functions, definition, root, skill_id, *, reactive=False, soul=False,
              inherited=(), trigger='', path=(), skill_props=None):
        for node in functions:
            if not isinstance(node, dict) or _number(node.get('Rate'), 1) <= 0:
                continue
            conditions = (*inherited, *_conditions(node))
            event = node.get('Trigger') or trigger
            # BeforeSkill is part of a selectable cast; other events require a
            # battle trigger and must not be described as unconditional openers.
            reaction = reactive or event not in ('', 'BeforeSkill', 'AfterSkill')
            crit = any(c.get('CheckFun') == 'IsCriticalHit' and not c.get('Invert') for cond in conditions for c in _walk(cond))
            camp, target = _target(node, definition)
            allied = camp == 'My' or target == 'Self'
            fun, fid = node.get('Fun'), str(node.get('FID') or '')
            kind = None
            if fun == 'AddTime':
                if allied and target != 'Self' and _sign(node) > 0:
                    kind = 'pull'
                elif not allied and _sign(node) < 0:
                    kind = 'push'
            elif fun in ('AddStatus', 'RandomAddStatus'):
                ids = node.get('FIDs') or [fid]
                if allied and target != 'Self' and any(tables['Status'].get(key, {}).get('Group', key) == 'IncreaseSpeed' for key in ids):
                    kind = 'haste'
                elif not allied:
                    kind = next((value for key in ids if (value := _status_kind(key, tables))), None)
            elif allied and target != 'Self' and (fun in ('ClearSkillCoolDown', 'ClearAllSkillCoolDown') or fun in ('AddSkillCoolDown', 'AddAllSkillCoolDown') and _sign(node) < 0):
                kind = 'refresh'
            elif not allied and fun in ('AddSkillCoolDown', 'AddAllSkillCoolDown', 'ReSetAllSkillCoolDown') and (fun.startswith('ReSet') or _sign(node) > 0):
                kind = 'cooldown'
            elif not allied and (fun in ('RemoveBuf', 'TransferBuff') or fun == 'AddAllBuffRound' and _sign(node) < 0):
                kind = 'strip'
            if kind:
                ignore = _number(node.get('IgnoreResist')) >= 1 or _number((skill_props or {}).get('IgnoreResist')) >= 1
                effects.append(dict(kind=kind, label=LABELS[kind], skillId=skill_id, rootSkill=root,
                                    target=target, conditional=bool(conditions) or reaction or skill_id != root,
                                    reactive=reaction, criticalTrigger=crit, soulOnly=soul,
                                    requiresHit=not allied and not ignore, ignoreResistance=not allied and ignore,
                                    rate=round(_number(node.get('Rate'), 1) * 100, 1),
                                    conditions=list(conditions), trigger=event,
                                    soulCost=int(tables['Skill'].get(root, {}).get('SkillSoulCost') or 0) if soul else 0))
            visit(node.get('SubFuns') or [], definition, root, skill_id, reactive=reaction, soul=soul,
                  inherited=conditions, trigger=event, path=path, skill_props=skill_props)
            if fun in REFERENCES and fid in tables['Skill'] and fid not in path and len(path) < 6:
                record = records.get(fid) or {'StaticID': fid, 'Level': 1}
                setting = _skill_setting(role, record, tables)
                visit(setting.functions, tables['Skill'][fid], root, fid, reactive=reaction or fun == 'CounterSkill', soul=soul,
                      inherited=(*conditions, {'Reference': fun}), trigger=event, path=(*path, fid), skill_props=setting.values)
            # A self status may contain a delayed skill/stance. Its effects are
            # conditional and retain their trigger; negative effects on self
            # never become an enemy-control recommendation.
            if fun == 'AddStatus' and fid in tables['Status'] and ('status', fid) not in path and len(path) < 6:
                props = direct_properties(tables['Status'][fid].get('Dynamic', {}))
                nested = props.get('Fun') or []
                if isinstance(nested, dict):
                    nested = [nested]
                visit(combine_functions(nested), definition, root, skill_id, reactive=True, soul=soul,
                      inherited=(*conditions, {'Status': fid}), trigger=event, path=(*path, ('status', fid)))

    for skill_id, hidden in roots:
        if hidden:
            continue
        definition = tables['Skill'].get(skill_id, {})
        record = records.get(skill_id) or {'StaticID': skill_id, 'Level': 1}
        setting = _skill_setting(role, record, tables)
        passive = definition.get('SkillType') == 'Passive'
        if passive:
            passive_props.update(setting.values)
        visit(setting.functions, definition, skill_id, skill_id, reactive=passive, path=(skill_id,), skill_props=setting.values)
        if not passive and int(definition.get('SkillSoulCost') or 0) > 0:
            visit(_soul_functions(role, record), definition, skill_id, skill_id, soul=True, path=(skill_id,), skill_props=setting.values)
    # Soul casts repeat most normal effects; retain only actual new bypasses.
    normal = {(e['rootSkill'], e['skillId'], e['kind'], e['ignoreResistance']) for e in effects if not e['soulOnly']}
    effects = [e for e in effects if not e['soulOnly'] or e['ignoreResistance'] and (e['rootSkill'], e['skillId'], e['kind'], True) not in normal]
    distinct = {json.dumps(e, sort_keys=True, ensure_ascii=False): e for e in effects}
    return dict(effects=list(distinct.values()), noCritical=bool(passive_props.get('NoCritical')),
                speedPenalty=_number(passive_props.get('SpeedFinalRate')) < 0)


@lru_cache(maxsize=1024)
def _cached(key):
    return _analyze(json.loads(key))


def analyze(role):
    snapshot = {key: role.get(key) for key in ('StaticID', 'AwakenLV', 'Skills')}
    return copy.deepcopy(_cached(json.dumps(snapshot, sort_keys=True)))


def profiles(role, damage_stat='Attack'):
    audit = analyze(role)
    normal = [e for e in audit['effects'] if not e['soulOnly']]
    on_cast = [e for e in normal if not e['reactive']]
    allied = [e for e in on_cast if e['kind'] in ('pull', 'haste', 'refresh') and not e['criticalTrigger']]
    crit_pull = [e for e in normal if e['kind'] == 'pull' and e['criticalTrigger']]
    hit = [e for e in on_cast if e['requiresHit']]
    bypass = [e for e in on_cast if e['ignoreResistance']]
    soul = [e for e in audit['effects'] if e['soulOnly'] and not e['reactive'] and e['ignoreResistance']]
    result = []

    def add(pid, title, evidence, *, tie=('HP', 'Defence'), hard=(), hit_target=False, ranking='speed-first', note=''):
        kinds = list(dict.fromkeys(e['label'] for e in evidence))
        skills = '、'.join(dict.fromkeys('技能' + e['rootSkill'].split('S')[-1] for e in evidence))
        if ranking == 'hit-first':
            rule = '先堆状态命中，同命中再比速度。'
        elif hit_target:
            rule = '命中达标后先堆速度，同速再比命中。'
        elif hard:
            rule = '暴击率至少 100%，先速度，同速比较输出。'
        else:
            rule = '速度第一，同速比较生存；此用途不要求暴击或状态命中。'
        conditional = '部分效果需触发，见用途依据。' if any(e['conditional'] for e in evidence) else ''
        summary = '、'.join(kinds[:3]) + ('等' if len(kinds) > 3 else '')
        result.append(dict(id=pid, title=title, description=f'{skills}：{summary}。{rule}{conditional}{note}',
                           priorities=['EffectHitRate', 'Speed', 'HP'] if ranking == 'hit-first' else ['Speed', *tie][:3],
                           damageStat=damage_stat, critical=bool(hard), ranking=ranking, tieBreak=list(tie),
                           hardConditions=list(hard), hitTarget=hit_target, evidence=evidence,
                           source='当前技能等级、觉醒与原始技能/状态机制；按用途推导'))

    if not audit['speedPenalty']:
        if allied and str(role.get('StaticID')) != 'H182':
            add('speed-pull', '最高速度·拉条提速' if any(e['kind'] in ('pull', 'haste') for e in allied) else '最高速度·冷却辅助', allied)
        if crit_pull and not audit['noCritical']:
            add('speed-crit', '最高速度＋百暴', crit_pull, tie=('damage', 'HP'), hard=({'stat': 'CriticalRate', 'min': 100, 'max': None},))
        if hit:
            add('speed-hit', '最高速度＋命中', hit, tie=('EffectHitRate', 'HP', 'Defence'), hit_target=True)
        if bypass:
            add('speed-ignore', '最高速度·无视抗性', bypass, note='仅所列效果无视抗性，仍须注意闪避、免疫及其他技能效果。')
        if soul:
            costs='、'.join(str(n) for n in sorted({e['soulCost'] for e in soul}))
            add('speed-soul', '烧魂抢速·无视抗性', soul, note=f'须烧魂（{costs}点）；仅所列效果无视抗性。未烧魂时请选命中用途。')
    if hit:
        add('hit-speed', '高命中＋速度', hit, tie=('Speed', 'HP', 'Defence'), hit_target=True, ranking='hit-first',
            note='适合命中优先的控场，并非最高速用途。')
    return result
