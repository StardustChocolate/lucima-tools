"""Server-confirmed friendship farming in Fictional Abyss floor one.

Carriers and each passenger batch have separate durable asset journals.
Friendship belongs to a StaticID, so duplicates never extend the queue.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from itertools import combinations
import json
import os
from pathlib import Path
import ssl
import threading

from . import abyss_loadout, abyss_progression, abyss_team
from .abyss_state import enabled, fantasy_config, metadata
from .game_client import GameError, is_transient_failure
from .tasks import _oid, item_name, normalize_abyss_config, abyss_scenes
from equipment import bonds, plans, recommend, stats

SCENE_ID = 'MysteriousRealm_1'
WAVES = ('upper', 'lower')
MAX_LEVEL = 10
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 30
HALF_FAILURE_LIMIT = 2
SINGLE_CARRIER_SWITCH_LIMIT = 5


class MissingRoleMetadata(GameError):
    pass


def retryable_transport(error):
    """Retry transport/edge failures, preserving explicit game/auth/storage errors."""
    cause, seen = error, set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, (ssl.SSLCertVerificationError, ssl.CertificateError)):
            return False
        cause = cause.__cause__ or cause.__context__
    return is_transient_failure(error)


def friendship_advanced(before, rows, static_ids):
    # Within-level experience goes back down on level-up. Compare level first;
    # a stale lower-level snapshot must never be treated as earned progress.
    return any(row['staticId'] in static_ids and row['staticId'] in before
               and (row['intimacyLevel'], row['intimacyExp']) > before[row['staticId']]
               for row in rows)


@lru_cache(maxsize=1)
def intimacy_levels():
    """Native LineRoleInfo .cctor list; getter indexes it by IntimacyLV - 1."""
    root = Path(os.environ.get('ARK_ASSET_ROOT') or Path(__file__).resolve().parents[1])
    try:
        value = json.loads((root / 'assets/intimacy_levels.json').read_text(encoding='utf-8'))
        levels = {int(level): int(cost) for level, cost in value['expToNextLevel'].items()}
        if value.get('schema') != 1 or value.get('maxLevel') != MAX_LEVEL or set(levels) != set(range(1, MAX_LEVEL)) or any(cost <= 0 for cost in levels.values()):
            raise ValueError('invalid friendship table')
        return levels
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise GameError('亲密度升级资料缺失或异常，请更新资源后重试') from error


def passenger_key(client, account_key, wave):
    identity = account_key or getattr(client, 'login_id', None) or getattr(client, 'aid', None)
    if not identity:
        raise GameError('缺少亲密度任务账号标识')
    return f'{identity}::intimacy-passengers::{wave}'


def recovery_pending(client, account_key=None):
    return (abyss_loadout.pending(client, account_key) or
            passenger_recovery_pending(client, account_key))


def passenger_recovery_pending(client, account_key=None):
    if not (account_key or getattr(client, 'login_id', None) or getattr(client, 'aid', None)):
        return False
    return any(abyss_loadout.pending(client, passenger_key(client, account_key, wave)) for wave in WAVES)


def restore_all(client, *, account_key=None, retry_deferred=False):
    """Restore one final baseline, including assets shared by nested loans."""
    result=abyss_loadout.restore_many(client,[account_key,*[passenger_key(client,account_key,wave) for wave in WAVES]],
                                    defer_no_role=True,retry_deferred=retry_deferred,defer_unreturned=True)
    fallback=result.get('fallback')or{}
    return {**result,'fallbacks':[fallback] if any(fallback.values()) else []}


def recover_before_start(client, *, account_key=None):
    return abyss_loadout.isolate_rejected(client,[account_key,*[passenger_key(client,account_key,wave) for wave in WAVES]])


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def friendship(state):
    """Use line-role Info in preference to combat-instance fields."""
    output = {}
    for role in (state.get('RoleDataContainer') or {}).get('Roles') or []:
        if 'IntimacyLV' in role and 'Intimacy' in role:
            output[str(role.get('StaticID') or '')] = role
    for row in (state.get('LineRoleDataContainer') or {}).get('RoleList') or []:
        info = row.get('Info') or row
        if info.get('StaticID'):
            output[str(info['StaticID'])] = info
    result = {}
    for sid, info in output.items():
        level, exp = _number(info.get('IntimacyLV')), _number(info.get('Intimacy'))
        if level is None or exp is None:
            continue
        maximum = next((_number(info[key]) for key in ('IntimacyToNextLevel', 'IntimacyMax', 'MaxIntimacy')
                        if _number(info.get(key)) not in (None, 0)), None)
        if maximum is None:
            maximum = intimacy_levels().get(level, 0 if level >= MAX_LEVEL else None)
        result[sid] = {'intimacyLevel': level, 'intimacyExp': exp, 'intimacyMax': maximum}
    return result


def eligible(state):
    """Actual owned heroes, using native InitStar rather than promoted Star."""
    table = metadata()['tables']['Role']
    relation = friendship(state)
    by_static = {}
    for role in (state.get('RoleDataContainer') or {}).get('Roles') or []:
        sid, rid = str(role.get('StaticID') or ''), _oid(role.get('_id'))
        definition = table.get(sid, {})
        if rid and sid.startswith('H') and not definition:
            raise MissingRoleMetadata(f'{item_name(sid)}缺少初始星级资料，正在等待自动同步')
        if not rid or not enabled(definition.get('IsHero')) or _number(definition.get('InitStar')) not in (4, 5):
            continue
        if sid not in relation:
            raise GameError(f'{item_name(sid)}缺少服务器亲密度记录，请刷新账号后重试')
        prior = by_static.get(sid)
        if prior is None or (int(role.get('LV') or 0), rid) > (int(prior.get('LV') or 0), _oid(prior['_id'])):
            by_static[sid] = role
    return [dict(roleId=_oid(role['_id']), staticId=sid, name=item_name(sid),
                 initialStar=_number(table[sid]['InitStar']), **relation[sid])
            for sid, role in sorted(by_static.items())]


def carrier_ids(carriers, wave=None):
    """Accept the original single-carrier format and mixed one/two-carrier halves."""
    values = [carriers.get(wave)] if wave else carriers.values()
    return [rid for value in values for rid in
            ([value] if isinstance(value, str) else value or []) if rid]


def _carrier_options(state, *, cancelled=None):
    """Rank real Lv60 damage roles against each half's rules and boss."""
    candidates = []
    ranking_state = abyss_team.selection_state(state)
    for wave in fantasy_config(SCENE_ID, state)['Waves']:
        rule = abyss_team.wave_policy(wave)
        enemy, monsters = abyss_team._enemy_traits(wave)
        bosses = [row for row in monsters if row['isBoss']] or monsters
        choices = []
        for role in (state.get('RoleDataContainer') or {}).get('Roles') or []:
            if cancelled and cancelled():
                raise GameError('自动刷亲密度已中止')
            rid, sid = _oid(role.get('_id')), str(role.get('StaticID') or '')
            definition = metadata()['tables']['Role'].get(sid, {})
            if not rid or int(role.get('LV') or 0) != 60 or not enabled(definition.get('IsHero')):
                continue
            job, element = definition.get('Job'), definition.get('Element')
            if job in rule['forbiddenJobs'] or element in rule['forbiddenElements']:
                continue
            traits = abyss_team.role_traits(role, seal_passive='SealPassive' in rule['flags'])
            combat = abyss_team.combat_role(role, traits=traits)
            if combat['kind'] not in ('single', 'area'):
                continue
            worn, _ = plans.worn_items((state.get('EquipmentContainer') or {}).get('Datas') or [], rid)
            panel = stats.role_panel(ranking_state, role, worn, include_artifact_effects=True)['panel']
            matches = [abyss_team.element_matchup(element, row.get('Element')) for row in bosses]
            if traits['forceConquer']:
                matches = [max(value, 1) for value in matches]
            preferred = abyss_team.preferred_main(role, rule, traits)
            if preferred:
                matches = [0 for value in matches]
            fit = 4 * (job in rule['preferredJobs']) + 4 * (element in rule['preferredElements'])
            fit += 8 * (job in rule['requiredJobs']) + 8 * (element in rule['requiredElements'])
            for flag in rule['desiredFlags']:
                fit += 8 * bool(flag in traits['flags'] or (flag == 'AOE' and traits['aoe'])
                                or (flag == 'HPScale' and traits['scale'] == 'HP')
                                or (flag == 'SP' and definition.get('SPType'))
                                or (flag == 'NonAttack' and traits['nonAttack']))
            fit += abyss_team._stage_coverage([{'id': rid, 'traits': traits, 'combat': combat}], rule)
            fit -= 10 * sum(flag in traits['flags'] for flag in rule['flags'])
            fit -= 6 * ('SealPassive' in rule['flags'] and traits['passive'])
            fit -= 15 * ('NoSP' in rule['flags'] and bool(definition.get('SPType')))
            fit -= 20 * ('PunishNonAttack' in rule['flags'] and traits['nonAttack'])
            immunity = rule['antiDebuffs'] | enemy['immune']
            counters = int(traits['break'] and 'ReduceDef' not in immunity)
            counters += int(enemy['recovery'] and traits['antiHeal'] and 'Unhealable' not in immunity)
            counters += int(enemy['protection'] and traits['blockBuff'] and 'CannotBuff' not in immunity)
            counters += int((enemy['protection'] or enemy['buffs']) and traits['strip'])
            output = recommend.metric(panel, combat['profile'])
            choices.append({'id': rid, 'sid': sid, 'ruleFit': fit, 'counters': counters,
                            'advantage': sum(value > 0 for value in matches),
                            'disadvantage': sum(value < 0 for value in matches),
                            'quality': output, 'outputTier': abyss_team._output_tier(combat),
                            'training': abyss_team.training_preference(role), 'preferredMain': preferred})
        candidates.append(choices)
    if len(candidates) != 2:
        raise GameError('第1关上下半配置异常，无法自动刷亲密度')
    return dict(zip(WAVES, candidates))


def _carrier_score(pair):
    return (sum(r.get('preferredMain', False) for r in pair),
            sum(r['ruleFit'] for r in pair), -sum(r['disadvantage'] for r in pair),
            sum(r['advantage'] for r in pair), sum(r['counters'] for r in pair),
            -sum(r['outputTier'] for r in pair),
            min(r['quality'] for r in pair), sum(r['quality'] for r in pair),
            tuple(r['training'] for r in pair), tuple(r['id'] for r in pair))


def normalize_mains(state, mains=None, *, auto_configure=True):
    """Validate explicitly chosen mains without silently replacing them."""
    if mains is None:mains={}
    if not isinstance(mains,dict) or set(mains)-set(WAVES):
        raise GameError('主角色配置格式异常')
    roles={_oid(role['_id']):role for role in (state.get('RoleDataContainer')or{}).get('Roles')or[]}
    chosen={};used=set()
    rules=dict(zip(WAVES,fantasy_config(SCENE_ID,state)['Waves']))
    for wave in WAVES:
        rid=mains.get(wave)
        if rid is None or rid=='':continue
        if not isinstance(rid,str) or rid not in roles:
            raise GameError('所选主角色不在当前账号中，请重新选择')
        role=roles[rid];definition=metadata()['tables']['Role'].get(role.get('StaticID'),{})
        if not enabled(definition.get('IsHero')):raise GameError('主角色必须为已拥有的英雄')
        if role['StaticID'] in used:raise GameError('上半、下半不能选择相同主角色')
        rule=abyss_team.wave_policy(rules[wave])
        if definition.get('Job') in rule['forbiddenJobs'] or definition.get('Element') in rule['forbiddenElements']:
            raise GameError('所选主角色不符合第1关对应半区的职业或属性规则')
        chosen[wave]=rid;used.add(role['StaticID'])
    if not auto_configure and len(chosen)!=2:raise GameError('请选择上下半主角色，或开启自动配队')
    return chosen


def choose_carriers(state, *, cancelled=None, excluded=(), selected=None):
    """Pick one damage role per half using active effects and boss mechanisms."""
    if selected:
        selected=normalize_mains(state,selected)
        if len(selected)==2:
            if tuple(selected[wave] for wave in WAVES) in set(excluded):
                raise GameError('所选主角色无法完成配装，请更换主角色或调整装备')
            return dict(selected)
    options = _carrier_options(state, cancelled=cancelled)
    if selected:
        roles={_oid(role['_id']):role for role in state['RoleDataContainer']['Roles']}
        wave=next(wave for wave in WAVES if wave not in selected)
        other=next(iter(selected.values()));sid=roles[other]['StaticID']
        candidates=[row for row in options[wave] if row['sid']!=sid
                    and tuple(({**selected,wave:row['id']})[half] for half in WAVES) not in set(excluded)]
        choice=max(candidates,key=lambda row:_carrier_score([row]),default=None)
        if choice is None:raise GameError('未选择的半区没有符合规则的 Lv60 输出主角色')
        return {**selected,wave:choice['id']}
    candidates = [options[wave] for wave in WAVES]
    excluded = set(excluded)
    pairs = ((a, b) for a in candidates[0] for b in candidates[1]
             if a['sid'] != b['sid'] and (a['id'], b['id']) not in excluded)
    selected = max(pairs, key=_carrier_score, default=None)
    if selected is None:
        raise GameError('需要两名不同且符合当前深渊规则的 Lv60 输出角色，无法找到合适主角色')
    return {wave: row['id'] for wave, row in zip(WAVES, selected)}


def choose_half_carriers(state, carriers, wave, *, count=1, excluded=(), cancelled=None):
    """Change only the failed half, without taking the other half's main heroes."""
    roles = {_oid(role['_id']): role for role in state.get('RoleDataContainer', {}).get('Roles', [])}
    other_sids = {roles[rid]['StaticID'] for half in WAVES if half != wave
                  for rid in carrier_ids(carriers, half) if rid in roles}
    choices = [row for row in _carrier_options(state, cancelled=cancelled)[wave]
               if row['sid'] not in other_sids]
    excluded = {tuple(sorted(ids)) for ids in excluded}
    groups = (group for group in combinations(choices, count)
              if len({row['sid'] for row in group}) == count
              and tuple(sorted(row['id'] for row in group)) not in excluded)
    selected = max(groups, key=_carrier_score, default=None)
    return [row['id'] for row in selected] if selected else None


def make_waves(state, carriers, previous=None):
    roles = {_oid(r['_id']): r for r in (state.get('RoleDataContainer') or {}).get('Roles') or []}
    mains = set(carrier_ids(carriers))
    carrier_sids = {roles[rid]['StaticID'] for rid in mains}
    all_rows = {row['roleId']: row for row in eligible(state)}
    queue = {row['roleId']: row for row in all_rows.values()
             if row['intimacyLevel'] < MAX_LEVEL and row['staticId'] not in carrier_sids}
    def level(rid):
        return int(roles[rid].get('LV') or 1)
    def tie(rid):
        return (-queue[rid]['intimacyLevel'], -queue[rid]['intimacyExp'], queue[rid]['staticId'])
    used = set(mains)
    waves = {}
    # A half's passenger batch stays until every member reaches 10. A second
    # main reduces passenger capacity to two without disturbing the other half.
    for wave in WAVES:
        old = [rid for rid in (previous or {}).get(wave, [])
               if rid in all_rows and rid not in used and all_rows[rid]['staticId'] not in carrier_sids]
        keep = old if any(all_rows[rid]['intimacyLevel'] < MAX_LEVEL for rid in old) else []
        keep = keep[:4-len(carrier_ids(carriers, wave))]
        used.update(keep)
        waves[wave] = [*carrier_ids(carriers, wave), *keep]
    for wave in WAVES:
        available=[rid for rid in queue if rid not in used]
        # Each new three-passenger batch gets one highest character level,
        # then two lowest character levels. Friendship only breaks equal-Lv ties.
        if len(waves[wave])==len(carrier_ids(carriers, wave)) and available:
            strongest=min(available,key=lambda rid:(-level(rid),*tie(rid)))
            waves[wave].append(strongest);used.add(strongest)
        for rid in sorted(available,key=lambda rid:(level(rid),*tie(rid))):
            if len(waves[wave]) >= 4:
                break
            if rid not in used:
                waves[wave].append(rid)
                used.add(rid)
        # Slot four receives the stage's position damage bonus when present.
        rule = abyss_team.wave_policy(fantasy_config(SCENE_ID, state)['Waves'][WAVES.index(wave)])
        if rule['positionDamage']:
            main = carrier_ids(carriers, wave)[0]
            waves[wave] = [rid for rid in waves[wave] if rid != main] + [None] * (4-len(waves[wave])) + [main]
        else:
            waves[wave] += [None] * (4 - len(waves[wave]))
    return waves


def protected_assets(state):
    """Sub-Lv60 heroes keep their exact gear and bonds, including as lenders."""
    owners={_oid(role['_id']) for role in (state.get('RoleDataContainer')or{}).get('Roles')or[]
            if int(role.get('LV')or 0)!=60}
    return ({_oid(item['_id']) for item in (state.get('EquipmentContainer')or{}).get('Datas')or[]
             if _oid(item.get('EquipRole')) in owners},
            {_oid(item['_id']) for item in bonds.inventory(state) if _oid(item.get('EquipRole')) in owners})


def full_level_ids(state, ids, *, exclude_equipment=(), exclude_artifacts=()):
    full={_oid(role['_id']) for role in (state.get('RoleDataContainer')or{}).get('Roles')or[]
          if int(role.get('LV')or 0)==60}
    held={_oid(item.get('EquipRole')) for items,excluded in
          (((state.get('EquipmentContainer')or{}).get('Datas')or[],set(exclude_equipment)),
           (bonds.inventory(state),set(exclude_artifacts)))
          for item in items if _oid(item['_id']) in excluded}
    return [rid for rid in ids if rid in full and rid not in held]


def plan_loadout(state, carriers, *, cancelled=None, progress=None, exclude_equipment=(), exclude_artifacts=(), fixed=None):
    """Equip 2–4 offensive mains, preserving successful halves' exact targets."""
    ids = full_level_ids(state,carrier_ids(carriers),exclude_equipment=exclude_equipment,exclude_artifacts=exclude_artifacts)
    if not ids:return None
    protected_equipment,protected_artifacts=protected_assets(state)
    exclude_equipment=set(exclude_equipment)|protected_equipment
    exclude_artifacts=set(exclude_artifacts)|protected_artifacts
    fixed = fixed or {}
    targets = {rid: group for rid, group in fixed.get('targets', {}).items() if rid in ids}
    artifacts = {rid: aid for rid, aid in fixed.get('artifactTargets', {}).items() if rid in targets}
    # A missing/rejected asset cannot freeze a new task. Replan that hero only.
    equipment_by_id = {_oid(item['_id']): item for item in (state.get('EquipmentContainer') or {}).get('Datas') or []}
    artifact_by_id = {_oid(item['_id']): item for item in bonds.inventory(state)}
    for rid in list(targets):
        if any(aid not in equipment_by_id or aid in exclude_equipment for aid in targets[rid]) or (artifacts.get(rid) and (artifacts[rid] not in artifact_by_id or artifacts[rid] in exclude_artifacts)):
            targets.pop(rid); artifacts.pop(rid, None)
    pending_ids=[rid for rid in ids if rid not in targets]
    value = abyss_loadout.plan(state, {wave: carrier_ids(carriers, wave) for wave in WAVES},
        cancelled=cancelled, progress=progress, offensive=True, utility_roles={},
        role_ids=pending_ids, equip_artifacts=True,
        exclude_equipment=set(exclude_equipment) | {aid for group in targets.values() for aid in group},
        exclude_artifacts=set(exclude_artifacts) | set(artifacts.values())) if pending_ids else {
            'targets':{},'artifactTargets':{},'equipment':{},'artifacts':{},'artifactBaseline':{}}
    value['targets'].update(deepcopy(targets))
    value['artifactTargets'].update(artifacts)
    value['roles'] = ids
    value['equipment'].update({aid: _oid(item.get('EquipRole')) or '' for aid, item in equipment_by_id.items()
        if aid in {eid for group in targets.values() for eid in group} or _oid(item.get('EquipRole')) in targets})
    value['artifactBaseline'].update({aid: _oid(item.get('EquipRole')) or '' for aid, item in artifact_by_id.items()
        if aid in artifacts.values() or _oid(item.get('EquipRole')) in targets})
    return value


def plan_passengers(state, carriers, waves, wave, active_plans, *, cancelled=None, progress=None,
                    exclude_equipment=(),exclude_artifacts=()):
    ids = full_level_ids(state,[rid for rid in waves[wave] if rid and rid not in carrier_ids(carriers)],
                         exclude_equipment=exclude_equipment,exclude_artifacts=exclude_artifacts)
    if not ids:
        return None
    equipment, artifacts = set(exclude_equipment), set(exclude_artifacts)
    protected_equipment,protected_artifacts=protected_assets(state)
    equipment.update(protected_equipment);artifacts.update(protected_artifacts)
    # Farming has first use of other heroes' outfits. Reserve only the actual
    # current party items; reverse all passenger loans before starting a new
    # batch so returning an item cannot displace another batch's borrowed gear.
    for loadout in active_plans.values():
        equipment.update(aid for group in loadout['targets'].values() for aid in group)
        artifacts.update(loadout.get('artifactTargets', {}).values())
    return abyss_loadout.plan(state, waves, cancelled=cancelled, progress=progress, offensive=True,
        utility_roles={}, role_ids=ids, equip_artifacts=True, survival_fallback=True,
        exclude_equipment=equipment, exclude_artifacts=artifacts)


def run(client, params):
    from .abyss_battle import BattleUnconfirmed, run_abyss_battle, check_battle_capacity
    cancel = params['_cancel_event']
    callback = params.get('_on_progress')
    account = params.get('_account_key')
    selected = {}
    rounds, stalled, waves, carriers = 0, 0, {}, {}
    first_clear_attempts = 0
    tried, locked, attempts, unconfirmed = set(), False, 0, 0
    halves = {wave: {'failures': 0, 'switches': 0, 'tried': set()} for wave in WAVES}
    needs_equip, fixed_loadout = False, None
    apply_recovery_attempts = 0
    current = {}
    active_plans = {}
    excluded_equipment=set();excluded_artifacts=set()
    state_lock = params.get('_account_lock')
    state_held = False
    result = {'ok': False, 'detail': '自动刷亲密度已停止'}

    def acquire_state(*, restoring=False):
        nonlocal state_held
        if state_lock is None or state_held:
            return
        while not state_lock.acquire(timeout=.1):
            if not restoring:
                check_stop()
        state_held = True

    def release_state():
        nonlocal state_held
        if state_held:
            state_held = False
            state_lock.release()

    def reserve_loans():
        client._intimacy_reserved_equipment = {
            aid for value in active_plans.values() for aid in value.get('equipment', {})}|excluded_equipment

    def publish(phase, message, **extra):
        nonlocal current
        had_lock = state_held
        acquire_state()
        try:
            source = client.account_state or {}
            state = {key: deepcopy(source.get(key) or {}) for key in
                     ('RoleDataContainer', 'LineRoleDataContainer')}
        finally:
            if not had_lock: release_state()
        try:
            rows = eligible(state)
        except MissingRoleMetadata:
            rows = []  # Recovery/status can render while the missing data downloads.
        by_id = {_oid(r['_id']): r for r in state.get('RoleDataContainer', {}).get('Roles', [])}
        relation = friendship(state)
        teams = {}
        for wave in WAVES:
            teams[wave] = []
            for rid in waves.get(wave, []):
                if not rid or rid not in by_id:
                    continue
                sid = by_id[rid]['StaticID']
                teams[wave].append(dict(roleId=rid, staticId=sid, name=item_name(sid),
                    carrier=rid in carrier_ids(carriers, wave), **relation.get(sid, {})))
        current = dict(phase=phase, message=message, sceneId=SCENE_ID, rounds=rounds,
                       attempts=attempts, carriersLocked=locked, triedLineups=len(tried),
                       total=len(rows), completed=sum(r['intimacyLevel'] >= MAX_LEVEL for r in rows),
                       remaining=sum(r['intimacyLevel'] < MAX_LEVEL for r in rows), teams=teams,
                       halfFailures={wave: halves[wave]['failures'] for wave in WAVES},
                       carrierSwitches={wave: halves[wave]['switches'] for wave in WAVES},
                       **extra)
        if callback:
            callback(deepcopy(current))

    def check_stop():
        if cancel.is_set():
            raise GameError('自动刷亲密度已中止')

    def wait_retry(count, message):
        seconds = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * max(1, count))
        publish('retrying', message + (f'，{seconds:g}秒后自动重试' if seconds else '，正在自动重试'), waiting=True)
        release_state()
        try:
            if cancel.wait(max(seconds, .05 if state_lock else 0)):
                check_stop()
        finally:
            acquire_state(restoring=True)

    def ensure_role_metadata():
        missing = {str(role.get('StaticID')) for role in
                   (client.account_state or {}).get('RoleDataContainer', {}).get('Roles', [])
                   if str(role.get('StaticID') or '').startswith('H') and
                   not metadata()['tables']['Role'].get(str(role.get('StaticID')), {}).get('InitStar')}
        if not missing:
            return False
        from . import resource_sync
        def resource_progress(message):
            if callback:
                callback({'phase': 'syncing_resources', 'message': message, 'waiting': True,
                          'rounds': rounds, 'teams': {}})
        release_state()
        try:
            resource_sync.ensure_roles(missing, account_key=account or getattr(client, 'login_id', None),
                                       cancel_event=cancel, on_progress=resource_progress)
        finally:
            acquire_state(restoring=True)
        return True

    def sync_state():
        failures = 0
        while True:
            check_stop()
            try:
                abyss_loadout.sync_owned_assets(client)
                check_stop()
                if ensure_role_metadata():
                    continue  # Re-read friendship after the resource generation changes.
                return
            except Exception as error:
                if not retryable_transport(error):
                    raise
                failures += 1
                wait_retry(failures, '账号信息同步暂时失败，保留当前队伍与借用资产')

    def restore_for_retry():
        publish('restoring', '正在处理借用资产，再更换失败半区的主输出')
        recovery = recover_before_start(client, account_key=account)
        if not recovery.get('restored'):
            raise GameError('借用装备/羁绊尚未完全归还，保留原始记录：'+str(recovery.get('error')or'等待核对'))
        excluded_equipment.update(recovery['equipment']);excluded_artifacts.update(recovery['artifacts'])
        active_plans.clear()
        reserve_loans()

    def equip_passenger_batch(wave):
        loadout = plan_passengers(client.account_state or {}, carriers, waves, wave, active_plans,
            cancelled=cancel.is_set, progress=lambda value: publish('equipping', value.get('message') or '正在配置陪刷角色装备与羁绊'),
            exclude_equipment=excluded_equipment,exclude_artifacts=excluded_artifacts)
        if loadout is not None:
            abyss_loadout.apply(client, loadout, account_key=passenger_key(client, account, wave), cancelled=cancel.is_set)
            active_plans[wave] = loadout
            reserve_loans()

    def recover_apply_error(error):
        nonlocal apply_recovery_attempts, needs_equip, fixed_loadout
        if not abyss_loadout.is_no_role(error):
            raise error
        publish('restoring','服务器返回 No Role，正在完整同步角色、装备与羁绊，核对后重新配装')
        recovery=recover_before_start(client,account_key=account)
        if not recovery.get('restored'):
            raise GameError('借装状态同步尚未确认：'+str(recovery.get('error')or'等待核对')) from error
        new_isolation=bool(recovery['equipment']-excluded_equipment or recovery['artifacts']-excluded_artifacts)
        # A transient rejection may recover without isolating anything. Permit
        # one fresh plan, but never spin forever on the same unchanged state.
        if not new_isolation and apply_recovery_attempts:
            raise GameError('完整同步后配装仍被服务器拒绝，未能定位可隔离资产：'+str(error)) from error
        apply_recovery_attempts=0 if new_isolation else apply_recovery_attempts+1
        excluded_equipment.update(recovery['equipment']);excluded_artifacts.update(recovery['artifacts'])
        active_plans.clear();fixed_loadout=None;needs_equip=True
        reserve_loans()
        if ensure_role_metadata():
            sync_state()
        publish('retrying','已同步核对角色归属，跳过仍异常的资产并继续配装')

    try:
        check_stop()
        acquire_state()
        client._intimacy_asset_owner = threading.get_ident()
        if recovery_pending(client, account) or any(abyss_loadout.deferred_assets(client,account).values()):
            publish('restoring','正在逐项归还上次借用的装备与羁绊')
            recovery = recover_before_start(client, account_key=account)
            if not recovery.get('restored'):
                raise GameError('上次借装尚未完全归还，正在保留原始记录：'+str(recovery.get('error')or'等待核对'))
            excluded_equipment.update(recovery['equipment']);excluded_artifacts.update(recovery['artifacts'])
            if recovery.get('warning'):publish('restoring',recovery['warning'])
        sync_state()
        check_stop()
        scene = next((scene for scene in abyss_scenes(client.account_state or {}) if scene['id'] == SCENE_ID), None)
        if scene is None:
            raise GameError('虚构深渊第1关尚未开放或未解锁')
        rows = eligible(client.account_state or {})
        selected=normalize_mains(client.account_state or {},params.get('mains'),
                                 auto_configure=params.get('autoConfigure',True) is True)
        if not scene['cleared'] and any(row['intimacyLevel'] < MAX_LEVEL for row in rows):
            publish('first_clear', '深渊第1关尚未通关，正在按深渊规则组队，通关后自动刷亲密度')

            def first_clear_progress(value):
                nonlocal waves
                if value.get('waves'):
                    waves = deepcopy(value['waves'])
                publish('first_clear', '先通关深渊第1关：' + (value.get('message') or '正在挑战'),
                        firstClearPhase=value.get('phase'), firstClearAttempt=value.get('attempt'))

            # Use the same full rule teams, loadouts, retry limit and settlement
            # checks as automatic Abyss, stopping at the first confirmed clear.
            protected_equipment,protected_artifacts=protected_assets(client.account_state or {})
            first_clear = abyss_progression.run(client, {
                'sceneId': SCENE_ID, 'waves': {'upper': [], 'lower': []}, 'autoAdvance': True,
                '_cancel_event': cancel, '_account_key': account, '_on_progress': first_clear_progress,
                '_recovery_excluded_equipment': excluded_equipment|protected_equipment,
                '_recovery_excluded_artifacts': excluded_artifacts|protected_artifacts,
            }, stop_after_clear=True)
            first_clear_attempts = len(first_clear.get('attempts') or [])
            check_stop()
            if not first_clear.get('ok'):
                raise GameError('深渊第1关首通未完成：' + first_clear.get('detail', '挑战失败'))
            waves = {}
            publish('preparing', '深渊第1关已通关，正在同步亲密度并准备陪刷队伍')
            sync_state()
            if not any(scene['id'] == SCENE_ID and scene['cleared'] for scene in abyss_scenes(client.account_state or {})):
                raise GameError('服务器尚未确认深渊第1关通关，请稍后重试')
            rows = eligible(client.account_state or {})
        while any(row['intimacyLevel'] < MAX_LEVEL for row in rows):
            check_stop()
            check_battle_capacity(client.account_state or {})
            if not carriers:
                publish('planning', '正在按第1关规则与BOSS选择主输出' + (f'，已尝试 {len(tried)} 组' if tried else ''))
                try:
                    choices={'selected':selected} if selected else {}
                    carriers = choose_carriers(client.account_state or {}, cancelled=cancel.is_set, excluded=tried,**choices)
                except GameError as error:
                    if tried and not cancel.is_set():
                        raise GameError(f'已尝试 {len(tried)} 组主角色，没有可继续尝试的合适输出组合：{error}') from error
                    raise
                tried.add(tuple(carriers[wave] for wave in WAVES))
                for wave in WAVES:
                    halves[wave]['tried'].add(tuple(sorted(carrier_ids(carriers, wave))))
                waves = make_waves(client.account_state or {}, carriers)
                needs_equip = True
            if not needs_equip and set(full_level_ids(client.account_state or {},carrier_ids(carriers),
                exclude_equipment=excluded_equipment,exclude_artifacts=excluded_artifacts)) != set((active_plans.get('carriers')or{}).get('targets',{})):
                restore_for_retry()
                needs_equip=True
                fixed_loadout=None
            if needs_equip:
                publish('equipping', '正在为主角色配置输出装备与羁绊，保留已通关半区的配装')
                try:
                    loadout = plan_loadout(client.account_state or {}, carriers, cancelled=cancel.is_set,
                        progress=lambda value: publish('equipping', value.get('message') or '正在配置输出装备与羁绊'),
                        exclude_equipment=excluded_equipment,exclude_artifacts=excluded_artifacts, fixed=fixed_loadout)
                except abyss_loadout.LoadoutUnavailable as error:
                    if fixed_loadout is not None:
                        raise
                    publish('retrying', str(error) + '，更换下一组主输出')
                    carriers = {}; waves = {}
                    continue
                check_stop()
                try:
                    if loadout is not None:
                        abyss_loadout.apply(client, loadout, account_key=account, cancelled=cancel.is_set)
                        active_plans['carriers'] = loadout
                    reserve_loans()
                    for wave in WAVES:
                        equip_passenger_batch(wave)
                except Exception as error:
                    recover_apply_error(error)
                    rows=eligible(client.account_state or {})
                    continue
                apply_recovery_attempts = 0
                needs_equip = False
                fixed_loadout = None
            next_waves = make_waves(client.account_state or {}, carriers, waves)
            changed_halves = [wave for wave in WAVES if next_waves[wave] != waves[wave]]
            for wave in WAVES:
                expected=set(full_level_ids(client.account_state or {},[rid for rid in next_waves[wave] if rid and rid not in carrier_ids(carriers)],
                    exclude_equipment=excluded_equipment,exclude_artifacts=excluded_artifacts))
                if expected!=set((active_plans.get(wave)or{}).get('targets',{})) and wave not in changed_halves:
                    changed_halves.append(wave)
            refresh_halves = [wave for wave in reversed(WAVES) if wave in active_plans] if changed_halves else []
            replan_after_return=False
            for wave in refresh_halves:
                publish('restoring', '本批培养角色或等级已更新，先归还陪刷借装，再配置装备与羁绊')
                recovery = abyss_loadout.recover_before_challenge(client, account_key=passenger_key(client, account, wave))
                excluded_equipment.update(recovery.get('unresolvedEquipment')or[])
                excluded_artifacts.update(recovery.get('unresolvedArtifacts')or[])
                if not recovery.get('restored'):
                    # A passenger's rejected return must follow the same skip
                    # policy as old carrier loans. Flatten all journals before
                    # borrowing again, so intermediate owners are not replayed.
                    restore_for_retry()
                    needs_equip=True;fixed_loadout=None;replan_after_return=True
                    rows=eligible(client.account_state or {})
                    break
                active_plans.pop(wave, None)
            if replan_after_return:continue
            waves = next_waves
            try:
                for wave in WAVES if changed_halves else []:
                    check_stop()
                    equip_passenger_batch(wave)
            except Exception as error:
                recover_apply_error(error)
                rows=eligible(client.account_state or {})
                continue
            apply_recovery_attempts = 0
            battle = normalize_abyss_config(client.account_state or {}, SCENE_ID, waves)
            before = {row['staticId']: (row['intimacyLevel'], row['intimacyExp']) for row in rows}
            role_sids = {_oid(role['_id']): role['StaticID'] for role in
                         (client.account_state or {}).get('RoleDataContainer', {}).get('Roles', [])}
            party_sids = {role_sids[rid] for team in waves.values() for rid in team if rid in role_sids}
            attempts += 1
            publish('battle', f'正在刷第1关 · 第 {rounds + 1} 轮')
            synced = False
            try:
                # The websocket owns its local battle units, not the account's
                # HTTP task lock. Shops/claims can run while the fight proceeds.
                release_state()
                try:
                    # This UI shows friendship after settlement, not live HP.
                    # Avoid rebuilding/transmitting a complete battle display
                    # on every combat action while farming in the background.
                    # Game-client 9x patches only scale local Unity animation;
                    # this protocol client already submits each action as soon
                    # as the server requests it, so there is no animation wait.
                    outcome = run_abyss_battle(client, {**battle, 'useSoul': True, 'prioritizeSoulBreak': True,
                        '_cancel_event': cancel})
                finally:
                    acquire_state(restoring=True)
                if outcome.get('ok') and not outcome.get('settlementConfirmed'):
                    raise BattleUnconfirmed('本轮结算帧尚未确认')
            except Exception as error:
                if not isinstance(error, BattleUnconfirmed) and not retryable_transport(error):
                    raise
                check_stop()
                publish('refreshing', '战斗连接或结算暂未确认，正在核对服务器亲密度', waiting=True)
                sync_state()
                rows = eligible(client.account_state or {})
                synced = True
                if friendship_advanced(before, rows, party_sids):
                    outcome = {'ok': True, 'settlementConfirmed': True}
                else:
                    unconfirmed += 1
                    wait_retry(unconfirmed, '已同步服务器，本轮亲密度尚未增加，保持当前角色继续刷取')
                    continue
            check_stop()
            unconfirmed = 0
            # Unplayed/unknown halves do not count as defeats. A half's win
            # resets only its own consecutive failure streak, even on a loss
            # in the other half. Switch counts remain independent for the run.
            half_results = {wave: True for wave in WAVES} if outcome.get('ok') else outcome.get('halfResults') or {}
            for wave in WAVES:
                if half_results.get(wave) is True:
                    halves[wave]['failures'] = 0
                elif half_results.get(wave) is False:
                    halves[wave]['failures'] += 1
            if not outcome.get('ok'):
                changing = [wave for wave in WAVES if half_results.get(wave) is False
                            and halves[wave]['failures'] >= HALF_FAILURE_LIMIT and wave not in selected]
                if changing:
                    old_mains = set(carrier_ids(carriers))
                    retained = {wave: [rid for rid in waves[wave] if rid not in old_mains] for wave in WAVES}
                    healthy = {rid for wave in WAVES if wave not in changing for rid in carrier_ids(carriers, wave)}
                    main_plan = active_plans.get('carriers') or {}
                    fixed_loadout = {key: {rid: deepcopy(value) for rid, value in main_plan.get(key, {}).items() if rid in healthy}
                                     for key in ('targets', 'artifactTargets')}
                    restore_for_retry()
                    for wave in changing:
                        status = halves[wave]
                        count = 2 if len(carrier_ids(carriers, wave)) == 2 or status['switches'] >= SINGLE_CARRIER_SWITCH_LIMIT else 1
                        chosen = choose_half_carriers(client.account_state or {}, carriers, wave, count=count,
                            excluded=status['tried'], cancelled=cancel.is_set)
                        if chosen is None and count == 1:
                            # A small roster can exhaust single mains before
                            # five changes. Try two rather than halt farming.
                            chosen = choose_half_carriers(client.account_state or {}, carriers, wave, count=2,
                                excluded=status['tried'], cancelled=cancel.is_set)
                        if chosen is None:
                            chosen = choose_half_carriers(client.account_state or {}, carriers, wave, count=count,
                                cancelled=cancel.is_set) or carrier_ids(carriers, wave)
                        if len(chosen) == 1 and chosen != carrier_ids(carriers, wave):
                            status['switches'] += 1
                        carriers[wave] = chosen
                        status['tried'].add(tuple(sorted(chosen)))
                        status['failures'] = 0
                        label = '上半' if wave == 'upper' else '下半'
                        publish('planning', f'{label}连续失败2次，' + ('改用两名主输出' if len(chosen) == 2 else f'更换主输出（已更换{status["switches"]}次）'))
                    waves = make_waves(client.account_state or {}, carriers, retained)
                    needs_equip = True
                    rows = eligible(client.account_state or {})
                else:
                    wait_retry(max((status['failures'] for status in halves.values()), default=1),
                               '本轮未通关，固定使用所选主角色并继续重试' if any(wave in selected and half_results.get(wave) is False for wave in WAVES)
                               else '本轮未通关，保留已通关半区，失败半区连续失败2次后自动更换主输出')
                    sync_state()
                    rows = eligible(client.account_state or {})
                continue
            locked = True
            rounds += 1
            publish('refreshing', '正在读取本轮亲密度，每半队本批陪刷角色全部满10级后更换下一批')
            # SceneRewardData already contains the authoritative LineRoleDatas
            # (friendship level/exp). A full re-login after every battle is the
            # dominant slowdown; only fall back to it when the settlement frame
            # did not carry fresh friendship data.
            if not synced and not outcome.get('intimacySynced'):
                sync_state()
            check_stop()
            rows = eligible(client.account_state or {})
            changed = friendship_advanced(before, rows, party_sids)
            stalled = 0 if changed else stalled + 1
            publish('progress', '本轮亲密度已确认，继续使用已通关的主输出，失败按半区分别重试')
            if not any(row['intimacyLevel'] < MAX_LEVEL for row in rows):
                break
            if stalled >= 3:
                wait_retry(stalled - 2, '服务器暂未显示亲密度增长，本批角色尚未满10级，继续刷取')
        result = {'ok': True, 'detail': '所有已拥有的初始四星、五星英雄亲密度均已满10级'}
    except Exception as error:
        result = {'ok': False, 'cancelled': cancel.is_set(), 'detail': str(error)}
    finally:
        acquire_state(restoring=True)
        if recovery_pending(client, account):
            # Restoration must ignore the cancellation signal.
            if callback:
                try:callback({**current, 'phase': 'restoring', 'message': '正在归还主角色与陪刷角色借用的装备和羁绊'})
                except Exception:pass
            try:
                recovery = restore_all(client, account_key=account)
            except Exception as error:
                recovery = {'restored': False, 'error': str(error)}
            if not recovery['restored']:
                result.update(ok=False, recoveryRequired=True,
                    detail=result['detail'] + '；装备/羁绊尚未完全归还：' + recovery['error'])
            else:
                result['detail'] += ('；可处理的借装已归还或确认卸下，缺失资产已记录'
                                     if recovery.get('fallbacks') else '；本轮借用的装备与羁绊已归还')
                if recovery.get('deferred'):
                    result['detail']=result['detail'].removesuffix('；本轮借用的装备与羁绊已归还')
                    result['detail']+='；本轮可归还的借装已处理，其余已隔离'
        client._intimacy_asset_owner = None
        try:
            client._intimacy_reserved_equipment = abyss_loadout.reserved_equipment(client,account)
            deferred=abyss_loadout.deferred_assets(client,account)
            if any(deferred.values()):
                result['deferredAssets']={key:sorted(ids) for key,ids in deferred.items()}
                result['detail']+=f'；{sum(map(len,deferred.values()))} 件历史借装仍隔离保留原始归属，未计为已归还'
        finally:
            release_state()
    return {**result, 'rounds': rounds, 'attempts': attempts, 'firstClearAttempts': first_clear_attempts, 'progress': current}
