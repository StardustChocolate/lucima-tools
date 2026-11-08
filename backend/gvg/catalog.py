"""当前版本角色、实际技能语义与可追溯的 GVG 能力索引。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from ..abyss_state import metadata, direct_properties
from ..abyss_properties import _skill_setting, max_skill_level
from ..abyss_team import capability_nodes, is_break, status_group, walk
from ..tasks import item_name, _localization, _abyss_skill_text
from equipment import bonds
from equipment.recommend import profiles

ENGINE = 'gvg-rules-20261026.1'
PLAN_SHA = '88b9e41e44deb2962ee877e926ad7ce95d7818c735c2d7cf43e0950f6e0f9d7b'
ELEMENTS = {'Fire': '火', 'Ice': '水', 'Earth': '木', 'Light': '光', 'Dark': '暗'}
TRAITS = {'break': '破甲', 'cleanse': '净化', 'heal': '治疗/复活', 'protect': '保护',
          'strip': '驱散', 'control': '控制', 'pull': '队伍拉条', 'revive': '复活',
          'antiRevive': '阻止复活', 'extinction': '击杀灭绝', 'counter': '反击',
          'punishNonAttack': '反制非攻击技能', 'aoe': '群体攻击'}
RULES = {
    'teamSize': 3, 'halves': ['upper', 'lower'], 'uniqueStaticRole': True,
    'builds': 'current', 'positionState': 'unverified', 'initialBarState': 'unverified',
    'evidence': '规则建议', 'simulation': False,
    'notes': ['使用当前装备与羁绊，不自动换装或开战。',
              '序位用于展示；三人战实际站位与四号位效果尚待核实。',
              '配速只比较所填面板值；初始行动条、团队光环与触发拉条未做整场模拟。'],
}


def root():
    return Path(os.environ.get('ARK_ASSET_ROOT') or Path(__file__).resolve().parents[2])


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@lru_cache(maxsize=1)
def versions():
    equipment_root = Path(bonds.__file__).resolve().parent
    from ..resource_store import path as resource_path
    stats_hash = hashlib.sha256(resource_path('equipment/stat_tables.json').read_bytes()).hexdigest()
    profiles_hash = hashlib.sha256((equipment_root/'recommend_profiles.json').read_bytes()).hexdigest()
    return {'engine': ENGINE, 'game': metadata()['version'], 'tables': digest(metadata()['table_sha256']), 'profiles': profiles_hash,
            'panels': stats_hash, 'sourcePlan': PLAN_SHA}


def skill_views(role, *, unknown=False):
    levels = {s['StaticID']: int(s.get('Level') or 1) for s in (role.get('Skills') or {}).get('Skills') or []}
    tables = metadata()['tables']; loc = _localization(); rows = []
    for raw in tables['Role'].get(role['StaticID'], {}).get('SkillIDs', '').split('~'):
        sid, _, hidden = raw.partition(':')
        if not sid: continue
        maximum = max_skill_level(sid, tables)
        rows.append({'id': sid, 'name': loc.get('T_Skill_NAME_'+sid) or sid,
                     'level': None if unknown and sid not in levels else levels.get(sid, 1),
                     'maxLevel': maximum, 'hidden': hidden == 'Hide',
                     'passive': tables['Skill'].get(sid, {}).get('SkillType') == 'Passive',
                     'description': _abyss_skill_text(loc.get('T_Skill_Description_'+sid)) or '暂无独立说明'})
    return rows


def mechanics(role, *, unknown=False):
    """能力表示潜在用途，不代表触发、控制成功或已验证战斗胜利。"""
    tables = metadata()['tables']; facts = set(); sources = {}; issues = set()
    damage_stats = set(); critical_damage = False; records = skill_views(role, unknown=unknown)
    def mark(name, sid):
        facts.add(name); sources.setdefault(name, set()).add(sid)
    for record in records:
        sid = record['id']; definition = tables['Skill'].get(sid, {})
        # 敌方等级未知时仅归纳可能出现的机制；不以图鉴满级数值当实测属性。
        levels = [record['level']] if record['level'] is not None else sorted({1, record['maxLevel']})
        for level in levels:
            setting = _skill_setting(role, {'StaticID': sid, 'Level': level}, tables)
            passive = _skill_setting(role, {'StaticID': sid, 'Level': level}, tables, passive=True)
            direct = {**setting.values, **passive.values}
            if direct.get('EnemyCantRevive'): mark('antiRevive', sid)
            if definition.get('TargetCamp') != 'My' and not record['passive'] and not record['hidden']:
                if definition.get('TargetType') == 'All': mark('aoe', sid)
                critical_damage |= not bool(direct.get('NoCritical'))
            for node in capability_nodes(setting.functions, role):
                fun = node.get('Fun'); ally = node.get('TargetCamp', definition.get('TargetCamp', 'Enemy')) == 'My'
                team = ally and node.get('Target', definition.get('TargetType', 'One')) != 'Self'
                if fun == 'AddStatus':
                    fid = str(node.get('FID') or ''); group = status_group(fid)
                    if not ally and is_break(fid): mark('break', sid)
                    if not ally and group in ('Stun', 'Sleep', 'Freeze', 'Silence', 'Taunt', 'Seal', 'Restrict', 'Provoke', 'UnableAction'): mark('control', sid)
                    if team and group in ('Shield', 'Immune', 'Immortal', 'SkillNullifier', 'AddDefence', 'Stealth'): mark('protect', sid)
                    if group in ('Revive', 'Resurrection'): mark('revive', sid)
                    if ally and group in ('Counterattack', 'Counter'): mark('counter', sid)
                if fun in ('AddHP', 'Revive', 'Resurrection') and team: mark('heal', sid)
                if fun in ('Revive', 'Resurrection') and ally: mark('revive', sid)
                if fun == 'RemoveDeBuf' and team: mark('cleanse', sid)
                if fun in ('RemoveBuf', 'RemoveBufsByID') and not ally: mark('strip', sid)
                if fun in ('SplitDamage', 'Shield') and ally: mark('protect', sid)
                if fun == 'SetDieOut': mark('extinction', sid)
                if fun in ('CounterSkill', 'SetCounterSkill'): mark('counter', sid)
                if fun == 'AddTime' and team and isinstance(node.get('Value'), (int, float)) and node['Value'] > 0: mark('pull', sid)
                if node.get('Trigger') in ('AnyEnemyAfterSkill', 'AnyEnemyBeforeSkill') and any(n.get('CheckFun') == 'IsNoDamageSkill' for n in walk(node)):
                    mark('punishNonAttack', sid)
                prop = (node.get('ConstAttack') or {}).get('Property') if isinstance(node.get('ConstAttack'), dict) else None
                if prop in ('MaxHP', 'Defence'): damage_stats.add('HP' if prop == 'MaxHP' else 'Defence')
                if '__unparsed__' in node: issues.add(sid+'：存在未解析节点')
                if any(n.get('CheckFun') == 'Position' and str(n.get('CheckID')) == '3' for n in walk(node)):
                    issues.add(sid+'：依赖四号位，三人战映射未核实')
    known_profiles = profiles(role)
    if known_profiles and all(not p['critical'] for p in known_profiles): critical_damage = False
    return {'tags': sorted(facts), 'sources': {k: sorted(v) for k, v in sources.items()},
            'scale': 'Defence' if 'Defence' in damage_stats else 'HP' if 'HP' in damage_stats else 'Attack',
            'critical': critical_damage, 'issues': sorted(issues), 'skills': records}


@lru_cache(maxsize=1)
def catalog():
    index = json.loads((root()/'assets/encyclopedia/index.json').read_text(encoding='utf-8'))
    aliases = json.loads((Path(bonds.__file__).resolve().parent/'role_aliases.json').read_text(encoding='utf-8'))
    characters = []
    for item in index['characters']:
        sid = item['id']
        if sid not in metadata()['tables']['Role']: continue
        role = {'StaticID': sid, 'LV': 60}; facts = mechanics(role, unknown=True)
        avatar = item.get('avatar')
        if avatar and not (root()/avatar.lstrip('/')).is_file(): avatar = None
        characters.append({'id': sid, 'staticID': sid, 'name': item['name'], 'avatar': avatar,
            'aliases': list(dict.fromkeys([*(item.get('aliases') or []),*(aliases.get(sid, {}).get('aliases') or [])])),
            'job': item['job'], 'jobName': bonds.JOBS.get(item['job'], item['job']),
            'element': item['element'], 'elementName': ELEMENTS.get(item['element'], item['element']),
            'tags': facts['tags'], 'skills': facts['skills'], 'issues': facts['issues']})
    loc = _localization()
    artifacts = [{'id': sid, 'staticID': sid, 'name': item_name(sid), 'job': row.get('Job'),
                  'effect': _abyss_skill_text(loc.get('T_Artifact_FunDescription_'+sid)) or '效果说明暂缺'}
                 for sid, row in metadata()['tables']['Artifact'].items()]
    return {'schema': 1, 'versions': versions(), 'characters': characters, 'artifacts': artifacts,
            'traitNames': TRAITS, 'rules': copy.deepcopy(RULES)}
