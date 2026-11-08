"""严格录入校验与不含凭据的账号快照。"""
from __future__ import annotations

import copy
import math
from collections import Counter

from ..abyss_state import metadata
from ..game_client import GameError
from equipment import plans, stats, bonds
from .catalog import catalog, digest, mechanics, versions

HALVES = ('upper', 'lower')


def snapshot(client):
    return copy.deepcopy({key: (client.account_state or {}).get(key, {})
                          for key in ('RoleDataContainer', 'EquipmentContainer', 'Artifacts')})


def fingerprint(state):
    return digest(state)


def number(value, label, *, low=0, high=10000000):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise GameError(label+'必须是有效数字')
    try: value = float(value)
    except (ValueError, OverflowError): raise GameError(label+'必须是有效数字') from None
    if not math.isfinite(value) or not low <= value <= high: raise GameError(label+'超出有效范围')
    return value


def observation(raw, label, *, health=False):
    if raw is None: raw = {}
    if not isinstance(raw, dict): raise GameError(label+'格式无效')
    kind = raw.get('kind', 'unknown')
    kinds = ('unknown', 'exact', 'interval', 'lower', 'upper')
    if kind not in kinds: raise GameError(label+'类型无效')
    context = raw.get('context', 'max' if health else 'panel')
    if context not in (('max', 'current', 'percent', 'observed') if health else ('panel', 'estimated')):
        raise GameError(label+'来源类型无效')
    result = {'kind': kind, 'min': None, 'max': None, 'context': context,
              'source': str(raw.get('source') or '手动录入')[:80]}
    if kind == 'unknown': return result
    high = 100 if health and context == 'percent' else 10000000 if health else 2000
    low = 0 if health and context in ('current', 'percent', 'observed') else .01
    if kind == 'exact':
        value = number(raw.get('value'), label, low=low, high=high)
        result.update(min=value, max=value, value=value)
    else:
        for bound in ('min', 'max'):
            if kind == 'interval' or (kind == 'lower' and bound == 'min') or (kind == 'upper' and bound == 'max'):
                result[bound] = number(raw.get(bound), label, low=low, high=high)
        if kind == 'interval' and result['min'] > result['max']: raise GameError(label+'最低值不能高于最高值')
    return result


def normalize(body):
    if not isinstance(body, dict): raise GameError('GVG 输入无效')
    chars = {row['id']: row for row in catalog()['characters']}; artifacts = {row['id'] for row in catalog()['artifacts']}
    raw_halves = body.get('enemies')
    if not isinstance(raw_halves, dict): raise GameError('请填写敌方上半和下半队伍')
    result = {'target': str(body.get('target') or '本次对手').strip()[:80], 'enemies': {}}
    for half in HALVES:
        rows = raw_halves.get(half)
        if not isinstance(rows, list) or len(rows) != 3: raise GameError('敌方上下半必须各选择三名角色')
        seen = set(); units = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict): raise GameError('敌方角色格式无效')
            sid = row.get('staticID')
            if not isinstance(sid, str) or sid not in chars: raise GameError('请选择有效的敌方角色')
            if sid in seen: raise GameError('同半场不能重复选择敌方角色')
            seen.add(sid); artifact = row.get('artifact', 'unknown')
            if not isinstance(artifact, str) or artifact not in {'unknown', 'none', *artifacts}: raise GameError('敌方羁绊无效')
            if artifact not in ('unknown', 'none'):
                definition = metadata()['tables']['Artifact'][artifact]
                if definition.get('Job') not in ('Other', chars[sid]['job']): raise GameError('敌方羁绊与角色职业不匹配')
            level = row.get('artifactLevel')
            if level not in (None, ''):
                level = number(level, '羁绊等级', low=1, high=30)
                if not level.is_integer(): raise GameError('羁绊等级应为整数')
                level = int(level)
            else: level = None
            stats_input = row.get('stats') or {}
            if not isinstance(stats_input, dict): raise GameError('敌方属性格式无效')
            extra = {}
            for key, label in (('Defence', '防御'), ('ResistanceRate', '抗性'), ('EffectHitRate', '命中')):
                value = stats_input.get(key)
                extra[key] = None if value in (None, '') else number(value, label, high=100000 if key == 'Defence' else 2000)
            units.append({'staticID': sid, 'slot': index+1,
                'speed': observation(row.get('speed'), '速度'), 'health': observation(row.get('health'), '生命', health=True),
                'artifact': artifact, 'artifactLevel': level, 'stats': extra})
        result['enemies'][half] = units
    raw = body.get('options') or {}
    if not isinstance(raw, dict): raise GameError('配队设置无效')
    policy, goal = raw.get('policy', 'manual'), raw.get('goal', 'balanced')
    if policy not in ('manual', 'auto'): raise GameError('操作模式无效')
    if goal not in ('balanced', 'upper', 'lower'): raise GameError('配队目标无效')
    minimum = raw.get('minLevel', 60)
    if minimum not in (1, 50, 60) or isinstance(minimum, bool): raise GameError('角色等级设置无效')
    def ids(value):
        if not isinstance(value, list) or len(value) > 500 or any(not isinstance(x, str) or not x or len(x)>100 for x in value): raise GameError('我方角色设置无效')
        return sorted(set(value))
    locks = raw.get('locks') or {}
    if not isinstance(locks, dict): raise GameError('锁定角色格式无效')
    locks = {half: ids(locks.get(half, [])) for half in HALVES}
    excluded = ids(raw.get('excluded', []))
    if any(len(rows)>3 for rows in locks.values()): raise GameError('每半最多锁定三名角色')
    if set(locks['upper']) & set(locks['lower']) or set(excluded) & {*locks['upper'], *locks['lower']}:
        raise GameError('角色不能同时锁定到两半或同时排除')
    result['options'] = {'policy': policy, 'goal': goal, 'minLevel': minimum, 'locks': locks, 'excluded': excluded}
    return result


def roster(state, avatar=lambda sid: None):
    chars = {row['id']: row for row in catalog()['characters']}
    raw_roles = (state.get('RoleDataContainer') or {}).get('Roles') or []
    equips = (state.get('EquipmentContainer') or {}).get('Datas') or []
    gear_duplicates = {eid for eid, count in Counter(plans._oid(e.get('_id')) for e in equips).items() if count>1}
    role_duplicates = {rid for rid, count in Counter(plans._oid(r.get('_id')) for r in raw_roles).items() if count>1}
    artifact_duplicates = {aid for aid, count in Counter(plans._oid(a.get('_id')) for a in bonds.inventory(state)).items() if count>1}
    arts = {a['id']: a for a in bonds.catalog(state)}; result = []
    for raw in raw_roles:
        sid, rid = str(raw.get('StaticID') or ''), plans._oid(raw.get('_id'))
        if not rid or sid not in chars: continue
        row = {key: copy.deepcopy(value) for key, value in chars[sid].items() if key not in ('skills', 'tags', 'issues')}
        row.update(id=rid, staticID=sid, level=int(raw.get('LV') or 1), avatar=avatar(sid) or row.get('avatar'))
        gear, conflicts = plans.worn_items(equips, rid); worn = bonds.worn(state, rid); errors = []
        if rid in role_duplicates: errors.append('角色实例重复')
        if len(gear) != 6 or {plans.equip_slot(e) for e in gear} != set(plans.SLOTS): errors.append('当前装备不足六个部位')
        if conflicts: errors.append('同部位重复装备，需重新同步')
        if gear_duplicates.intersection(plans._oid(e.get('_id')) for e in gear): errors.append('装备实例重复')
        if len(worn)>1 or any(plans._oid(a.get('_id')) in artifact_duplicates for a in worn): errors.append('羁绊归属异常')
        row['artifact'] = arts.get(plans._oid(worn[0].get('_id'))) if worn else None
        if row['artifact'] and row['artifact']['job'] not in ('Other', row['job']): errors.append('羁绊职业不匹配')
        try:
            current = stats.role_panel(state, raw, gear, include_artifact_effects=True)
            if not current.get('panel') or any(not math.isfinite(v) for v in current['panel'].values()): raise GameError('面板资料不完整')
            row['current'] = current
        except (GameError, ValueError, TypeError, OverflowError, KeyError) as error: errors.append('面板数据无法完整核对：'+str(error))
        row['equipIds'] = [plans._oid(e.get('_id')) for e in gear]
        row['facts'] = mechanics(raw); row['skills'] = row['facts'].pop('skills')
        row['tags'] = row['facts']['tags']; row['issues'] = row['facts']['issues']
        row['unavailable'] = errors; row['ready'] = not errors
        result.append(row)
    result.sort(key=lambda row: (-row['ready'], -row['level'], row['staticID'], row['id']))
    return {'roles': result, 'snapshotHash': fingerprint(state), 'versions': versions()}
