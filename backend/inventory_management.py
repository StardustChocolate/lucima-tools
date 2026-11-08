"""Locked-copy-first character and bond management, with confirmed mutations.

Native RoleManager/ArtifactManager request fields, dump.cs 2604/2608/2613/
3653/3692/3696. ArtifactData starts at LV=0; Enhance=5 raises its
maximum level from 15 to 30, independently of current LV. Role SSS=7.
Never unlock anything, retry an uncertain mutation, or invent a success state.
"""
from copy import deepcopy

from .game_client import GameError
from .game_data import enabled
from .management_catalog import catalog

SMALL_WATER_SHIELD = 'H064'  # 西尔维纳，官方图鉴别名“小水盾”。


def oid(row):
    from .tasks import _oid
    return _oid(row.get('_id'))


def rows(client, kind):
    key, field = ('RoleDataContainer', 'Roles') if kind == 'roles' else ('Artifacts', 'Datas')
    result = (client.account_state or {}).get(key, {}).get(field)
    if not isinstance(result, list):
        raise GameError('背包资料不完整，请刷新账号后重试')
    return result


def integer(row, key):
    value = row.get(key)
    return value if type(value) is int and value >= 0 else None


def unlocked(row):
    # Unknown lock flags are protected, rather than treated as False.
    return row.get('IsLock') in (False, 0, '0', 'false', 'False')


def usable(row, kind):
    if not oid(row) or not unlocked(row):
        return False
    if kind == 'bonds':
        from .tasks import _oid
        # Native ArtifactData is untrained at LV=0, unlike RoleData.LV=1.
        # Requiring LV=1 skips fresh duplicates and can sell invested copies.
        return not _oid(row.get('EquipRole')) and integer(row, 'LV') == 0 and integer(row, 'Exp') == 0
    return (not row.get('Locks') and not enabled(row.get('IsDispatched'))
            and not enabled(row.get('IsFavorite')) and not row.get('ArtifactData')
            and not any((row.get('EquipmentMap') or {}).values()))


def _merge(client, kind, response, removed=()):
    key, field = ('RoleDataContainer', 'Roles') if kind == 'roles' else ('Artifacts', 'Datas')
    current = {oid(row): row for row in rows(client, kind)}
    updates = response.get('Roles') or [] if kind == 'roles' else [
        response.get(k) for k in ('Artifact', 'NewArtifact', 'OldInRoleArtifact')]
    for row in updates:
        if isinstance(row, dict) and oid(row):
            current.setdefault(oid(row), {}).update(deepcopy(row))
    for identifier in removed:
        current.pop(identifier, None)
    client.account_state[key][field] = list(current.values())
    if kind == 'bonds':
        # Equipped bonds also exist inside RoleData. Keep subsequent battle
        # payloads on the same confirmed Enhance/LV as the inventory copy.
        from .tasks import _oid
        role_rows = (client.account_state.get('RoleDataContainer') or {}).get('Roles') or []
        for update in response.get('Roles') or []:
            if isinstance(update, dict):
                old = next((r for r in role_rows if oid(r) == oid(update)), None)
                if old is not None:old.update(deepcopy(update))
        for artifact in updates:
            if not isinstance(artifact, dict) or not oid(artifact):
                continue
            for role in role_rows:
                nested = role.get('ArtifactData') or {}
                if oid(nested) == oid(artifact) or oid(role) == _oid(artifact.get('EquipRole')):
                    role['ArtifactData'] = deepcopy(artifact)


def mutate(client, kind, route, body, *, target=None, check=None, removed=()):
    from .tasks import _to_send_format
    client._inventory_management_uncertain = True
    response = client.call(route, client._auth_data(_to_send_format(body)))
    if not isinstance(response, dict):
        raise GameError('服务器未确认整理结果，请刷新账号核对')
    # Re-read authoritative inventory if the protocol omitted the target or
    # removal receipt. Do not pretend the requested mutation already happened.
    target_rows = response.get('Roles') or [] if kind == 'roles' else [
        response.get('Artifact'), response.get('NewArtifact')]
    returned = next((r for r in target_rows if isinstance(r, dict) and oid(r) == target), None)
    consumed = set(response.get('RemoveRoleIDs') or [])
    for entry in response.get('CostItems') or []:
        for field in ('RoleData', 'Role', 'Artifact'):
            item = entry.get(field) if isinstance(entry, dict) else None
            if isinstance(item, dict) and oid(item):
                consumed.add(oid(item))
    if (target and (not returned or not check(returned))) or not set(removed) <= consumed:
        client.bootstrap()
    else:
        _merge(client, kind, response, removed)
    actual = next((r for r in rows(client, kind) if oid(r) == target), None)
    if target and (not actual or not check(actual)):
        raise GameError('服务器未确认锁定或突破结果，已停止后续整理')
    if any(oid(r) in removed for r in rows(client, kind)):
        raise GameError('服务器未确认素材消耗或出售结果，已停止后续整理')
    client._inventory_management_uncertain = False
    return response


def library_rows(client, kind):
    data = (client.account_state or {}).get('LibraryContainer', {}).get('LibraryDataList')
    if not isinstance(data, list):
        raise GameError('保管仓库资料未同步，请刷新账号后重试')
    definitions = catalog()[kind]
    selected = []
    for row in data:
        item = row.get('ItemData') or {}
        if item.get('StaticID') not in definitions:
            continue
        count = integer(item, 'Count')
        if count is None or not oid(row):
            raise GameError('保管仓库数量或编号异常，已停止取回')
        if count:selected.append(row)
    return selected


def library_count(client, kind):
    return sum(row['ItemData']['Count'] for row in library_rows(client, kind))


def _bag_count(items, kind):
    from .tasks import _oid
    return len(items) if kind == 'roles' else sum(not _oid(r.get('EquipRole')) for r in items)


def snapshot(state):
    """Read-only card counters; unavailable fields stay unknown instead of zero."""
    state = state or {}
    limits = state.get('AccountSaveData') or {}
    library = (state.get('LibraryContainer') or {}).get('LibraryDataList')
    definitions = catalog() if isinstance(library, list) else {}
    result = {}
    for kind, container, field, limit in (
        ('roles', 'RoleDataContainer', 'Roles', 'RolePackMaxCount'),
        ('bonds', 'Artifacts', 'Datas', 'ArtifactPackMaxCount'),
    ):
        items = (state.get(container) or {}).get(field)
        count = _bag_count(items, kind) if isinstance(items, list) and all(isinstance(r, dict) for r in items) else None
        capacity = integer(limits, limit)
        if capacity == 0:capacity = None
        space = max(0, capacity - count) if capacity is not None and count is not None else None
        stored = None
        if isinstance(library, list):
            stored = 0
            for row in library:
                item = row.get('ItemData') if isinstance(row, dict) else None
                if not isinstance(item, dict):
                    stored = None;break
                if item.get('StaticID') in definitions.get(kind, {}):
                    amount = integer(item, 'Count')
                    if amount is None:
                        stored = None;break
                    stored += amount
        result[kind] = {'count': count, 'capacity': capacity, 'space': space, 'library': stored}
    return result


def bag_usage(client, kind):
    key = 'RolePackMaxCount' if kind == 'roles' else 'ArtifactPackMaxCount'
    maximum = ((client.account_state or {}).get('AccountSaveData') or {}).get(key)
    if type(maximum) is not int or maximum <= 0:
        raise GameError('背包容量未同步，已停止取回')
    items = rows(client, kind)
    # Native ArtifactContainer.IsArtifactFull counts only empty EquipRole.
    # Datas includes equipped bonds too; these do not occupy backpack slots.
    count = _bag_count(items, kind)
    return {'count': count, 'capacity': maximum, 'space': max(0, maximum - count)}


def bag_space(client, kind):
    return bag_usage(client, kind)['space']


def take_library(client, kind):
    """Native LibraryData.Clone(count), MaxTakeOutCount=40; never expand bags."""
    from .tasks import _to_send_format
    retrieved = 0
    while True:
        available = library_rows(client, kind)
        budget = min(40, bag_space(client, kind))
        if not available or not budget:return retrieved
        picks = []
        for row in available:
            count = min(budget, row['ItemData']['Count'])
            value = deepcopy(row);value['ItemData']['Count'] = count
            picks.append(value);budget -= count
            if not budget:break
        expected = sum(r['ItemData']['Count'] for r in picks)
        before = library_count(client, kind)
        old_ids = {oid(r) for r in rows(client, kind)}
        client._inventory_management_uncertain = True
        client.call('LibraryHandler.TakeOutLibrary', client._auth_data(_to_send_format({'LibraryDatas':picks})))
        # Authoritative snapshots confirm both removal from custody and actual
        # arrival in the bag, including partial stack withdrawals.
        client.bootstrap()
        actual = before - library_count(client, kind)
        added = {oid(r) for r in rows(client, kind)} - old_ids
        if actual != expected or len(added) != expected:
            raise GameError('仓库取回数量与背包到账未能核对，已停止；请刷新账号确认')
        client._inventory_management_uncertain = False
        retrieved += actual


def run(client, kind, options=None):
    """Settle the current bag, fill its free slots, and repeat until done."""
    if kind not in ('roles', 'bonds'):
        raise GameError('未知整理类型')
    options = options or {}
    if not isinstance(options, dict):
        raise GameError('整理选项格式无效')
    keep = {}
    for star in (3, 4):
        value = options.get(f'keep{star}', 2)
        if type(value) is not int or not 1 <= value <= 99:
            raise GameError('羁绊保留数量须为 1～99 张')
        keep[star] = value
    client.bootstrap()  # Refresh native locks/teams and any in-game bag expansion.
    client._inventory_management_uncertain = False
    definitions = catalog()[kind]
    # "upgraded" is the existing breakthrough-operation counter. Experience-
    # only feeding is forbidden, so no bonds are consumed for leveling.
    totals = {'locked':0, 'upgraded':0, 'levelingConsumed':0, 'sold':0, 'retrieved':0, 'rounds':0, 'rewardSummary':[]}
    retrieve = options.get('retrieveLibrary') is True
    blocked = False
    while True:
        # Include both the starting inventory and the final withdrawal in tidy.
        # A spare third copy must free its slot before the next retrieval.
        result = _tidy(client, kind, keep, definitions)
        for key in ('locked', 'upgraded', 'sold'):totals[key] += result[key]
        totals['rounds'] += 1
        totals['rewardSummary'].extend(result['rewardSummary'])
        remaining = library_count(client, kind) if retrieve else 0
        if not remaining:break
        if not bag_space(client, kind):
            blocked = True;break
        # This fills the available slots in confirmed batches of at most 40.
        # Return to tidy as soon as the bag is full or the library is empty.
        retrieved = take_library(client, kind)
        if not retrieved:
            blocked = True;break
        totals['retrieved'] += retrieved
    noun, action = ('团员', '传送') if kind == 'roles' else ('羁绊', '贩卖')
    state = '暂停' if blocked else '完成'
    detail = f"{noun}整理{state}：新加锁 {totals['locked']} 个，突破 {totals['upgraded']} 次，升级 {totals['levelingConsumed']} 个，{action} {totals['sold']} 个"
    usage = {}
    if retrieve:
        usage = bag_usage(client, kind)
        detail += f"；取回 {totals['retrieved']} 个，保管仓库剩余 {remaining} 个"
        detail += f"；背包占用 {usage['count']}/{usage['capacity']}，空位 {usage['space']}"
    if blocked:detail += '；背包已满且按保留规则无法继续腾出空间，请在游戏内整理后重试'
    return {'ok':not blocked, 'blocked':blocked, 'libraryRemaining':remaining, 'bag':usage, **totals, 'detail':detail}


def _tidy(client, kind, keep, definitions):
    groups = {}
    for row in rows(client, kind):
        sid = row.get('StaticID')
        if definitions.get(sid, {}).get('star') not in (3, 4) or not oid(row):
            continue
        if kind == 'roles' and sid == SMALL_WATER_SHIELD:
            continue
        groups.setdefault(sid, []).append(oid(row))
    locked = upgraded = sold = 0
    rewards = []
    # ArtifactData.NowMaxLV = 15 + 3 * Enhance: full breakthrough is Enhance=5.
    # Feeding grants XP too, but the base's current LV never gates breakthrough.
    field, maximum = ('ImprintLV', 7) if kind == 'roles' else ('Enhance', 5)
    def group(sid):
        return [r for r in rows(client, kind) if r.get('StaticID') == sid]
    for sid in sorted(groups):
        current = group(sid)
        bases = [r for r in current if enabled(r.get('IsLock'))]
        wanted = 1 if kind == 'roles' else keep[definitions[sid]['star']]
        choices = sorted([r for r in current if unlocked(r) and integer(r, field) is not None],
                         key=lambda r: (integer(r, field), integer(r, 'LV') or 0), reverse=True)
        for row in choices[:max(0, wanted - len(bases))]:
            from .tasks import _oid
            lock_body = {'RoleID': oid(row)} if kind == 'roles' else {
                'ArtifactID': oid(row), 'RoleID': _oid(row.get('EquipRole')) or ''}
            mutate(client, kind, 'RoleHandler.LockRole' if kind == 'roles' else 'ArtifactHandler.Lock',
                   lock_body, target=oid(row),
                   check=lambda r: enabled(r.get('IsLock')))
            locked += 1
        base_ids = [oid(r) for r in group(sid) if enabled(r.get('IsLock'))]
        for base_id in base_ids:
            while True:
                base = next(r for r in group(sid) if oid(r) == base_id)
                level = integer(base, field)
                if level is None:
                    raise GameError('突破等级资料异常，已停止整理')
                if level >= maximum:
                    break
                donors = [r for r in group(sid) if usable(r, kind) and integer(r, field) is not None]
                # Native role duplicates carry LV-min(InitStar-3)+1 imprints;
                # use one at a time, so every increase is server-confirmed.
                minimum = max(0, definitions[sid]['star'] - 3) if kind == 'roles' else 0
                baseline = max(level, minimum) if kind == 'roles' else level
                def contribution(r):
                    value = integer(r, field)
                    return 1 if value == 0 else value - minimum + 1
                donors = [r for r in donors if 0 < contribution(r) <= maximum - baseline]
                if not donors:
                    break
                donor = min(donors, key=lambda r: (integer(r, field), integer(r, 'LV') or 0))
                body = ({'UpgradRoleID': base_id, 'MaterialRoleIDs': [oid(donor)], 'MaterialItems': []}
                        if kind == 'roles' else {'ArtifactID': base_id, 'Materials': [{'Artifact': deepcopy(donor)}]})
                expected = baseline + contribution(donor)
                mutate(client, kind, 'RoleHandler.ImprintRole' if kind == 'roles' else 'ArtifactHandler.Upgrade',
                       body, target=base_id, removed=[oid(donor)],
                       check=lambda r, expected=expected: enabled(r.get('IsLock')) and integer(r, field) == expected)
                upgraded += 1
        # Preserve all spare copies until EVERY locked base is complete.
        bases = [r for r in group(sid) if enabled(r.get('IsLock'))]
        if len(bases) < wanted or any(integer(r, field) != maximum for r in bases):
            continue
        if not definitions[sid]['sell']:
            continue
        # Native untrained levels differ between roles and bonds. Protect
        # duplicates with invested levels/XP when selecting sale candidates.
        untouched_level = 1 if kind == 'roles' else 0
        candidates = [oid(r) for r in group(sid)
                      if usable(r, kind) and integer(r, 'LV') == untouched_level]
        for start in range(0, len(candidates), 20):
            batch = [r for r in group(sid) if oid(r) in candidates[start:start + 20] and usable(r, kind)]
            if not batch:
                continue
            ids = [oid(r) for r in batch]
            response = mutate(client, kind, 'RoleHandler.SellRoles' if kind == 'roles' else 'ArtifactHandler.SellArtifacts',
                {'RoleIDs': ids} if kind == 'roles' else {'CostItems': [{'Artifact': deepcopy(r)} for r in batch]}, removed=ids)
            from .equipment_sale import reward_summary
            rewards.extend(reward_summary(response))
            sold += len(ids)
    return {'ok': True, 'locked': locked, 'upgraded': upgraded, 'sold': sold, 'rewardSummary': rewards,
            }
