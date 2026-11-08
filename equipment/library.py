"""Read-only equipment quality and exact, successive inventory rankings."""
from collections import Counter
from itertools import product
import time

from backend.tasks import SET_CN
from . import plans, stats

REFERENCE_ROLE = {'StaticID': 'H153', 'LV': 60, 'AwakenLV': 6}
GRADES = (('满配', 62), ('高配', 57), ('达标', 48), ('潜力胚', 40), ('待提升', 0))

# User-supplied Lv60, fully awakened, no-bond panels. These are already
# awakened: do not call role_mods or use the quality conversion base here.
RANKING_REFERENCES = {
    'speed': ('兔女郎爱莉卡', dict(HP=5380, Attack=1119, Defence=483, Speed=129,
                               CriticalRate=23, CriticalDamageRate=150, EffectHitRate=0, ResistanceRate=0, PinchRate=3)),
    'hit': ('蜜茶多恩', dict(HP=5542, Attack=1057, Defence=532, Speed=118,
                          CriticalRate=15, CriticalDamageRate=150, EffectHitRate=60, ResistanceRate=0, PinchRate=3)),
    'resist': ('乌尔德', dict(HP=5254, Attack=649, Defence=694, Speed=105,
                           CriticalRate=15, CriticalDamageRate=150, EffectHitRate=0, ResistanceRate=50, PinchRate=3)),
    'survival': ('Aoi Hinamori', dict(HP=6267, Attack=984, Defence=638, Speed=117,
                                   CriticalRate=15, CriticalDamageRate=150, EffectHitRate=0, ResistanceRate=0, PinchRate=3)),
}


def _reference_base(key):
    return {stat: value / 100 if stat in stats.PERCENT_STATS else value
            for stat, value in RANKING_REFERENCES[key][1].items()}


def _ranking_pool(equips):
    """Take each inventory identity once, including locked and worn items."""
    out, seen = [], set()
    for equip in equips:
        eid = plans._oid(equip.get('_id'))
        if not eid or eid in seen or equip.get('LV') != 15 or plans.equip_slot(equip) not in plans.SLOTS:
            continue
        seen.add(eid)
        out.append(equip)
    return out


def _build(key, equips):
    mods = Counter()
    for equip in equips:
        mods.update(properties(equip, main=True))
    set_mods, active = stats.set_mods(Counter(e.get('Set') for e in equips))
    mods.update(set_mods)
    raw = stats.panel(_reference_base(key), mods)
    panel = stats.display(raw)
    # Rankings retain the fractional set contribution (e.g. 129 * .16).
    # The ordinary loadout/game panel keeps its existing integer display.
    for name in ('Speed', 'EffectHitRate', 'ResistanceRate'):
        panel[name] = round(raw[name] * (100 if name in stats.PERCENT_STATS else 1), 2)
    value = {'speed': panel['Speed'], 'hit': panel['EffectHitRate'], 'resist': panel['ResistanceRate'],
             'survival': panel['HP'] * (1 + panel['Defence'] / 300)}[key]
    sets = [f'{SET_CN.get(name, name)}套×{count}' for name, count in Counter(active).items()]
    return {'value': value, 'equipIds': [plans._oid(e.get('_id')) for e in equips],
            'panel': panel, 'speed': panel['Speed'], 'sets': sets}


def properties(equip, *, main=False):
    rows = (equip.get('SubProps') or {}).get('SourceValues') or []
    if main:
        rows = [equip.get('MainProp') or {}, *rows]
    values = Counter()
    for row in rows:
        key, value = row.get('PropertyType'), row.get('Value')
        if key and type(value) in (int, float):
            values[key] += value
    return values


def quality(equip):
    """Percentage-equivalent score, using the same fixed role for flat stats."""
    values = properties(equip)
    base = stats.role_base(REFERENCE_ROLE)
    points = {key: value * 100 for key, value in values.items() if key.endswith('Rate')}
    for key in ('Attack', 'Defence', 'HP'):
        points[key + 'Rate'] = points.get(key + 'Rate', 0) + values[key + 'Value'] / base[key] * 100
    offense = points.get('AttackRate', 0) + points.get('CriticalRate', 0) * 1.5 + points.get('CriticalDamageRate', 0) * 1.1
    tank = points.get('HPRate', 0) + points.get('DefenceRate', 0)
    dual = points.get('EffectHitRate', 0) + points.get('ResistanceRate', 0)
    speed = values['SpeedValue']
    score = round(offense + tank + dual + speed * 2, 1)
    return {'score': score, 'grade': next(name for name, threshold in GRADES if score >= threshold),
            'speed': speed, 'offense': offense, 'tank': tank, 'dual': dual}


def speed_builds(equips, *, require_speed=False, limit=5):
    """Five successive fastest complete builds, consuming each item once.

    Only Speed and Revenge change out-of-combat speed. For each slot retain
    the fastest item in each of those set categories and in all other sets.
    Enumerating at most 3**6 combinations is therefore exact for each round.
    """
    remaining = _ranking_pool(equips)
    base_speed = RANKING_REFERENCES['speed'][1]['Speed']
    bonuses = {key: value['rates'].get('SpeedRate', 0)
               for key, value in stats.tables()['sets'].items() if value.get('rates', {}).get('SpeedRate')}
    results = []
    for _ in range(limit):
        by_slot = {slot: {} for slot in plans.SLOTS}
        for equip in remaining:
            slot = plans.equip_slot(equip)
            category = equip.get('Set') if equip.get('Set') in bonuses else ''
            props = properties(equip, main=True)
            value = props['SpeedValue'] + base_speed * props['SpeedRate']
            old = by_slot[slot].get(category)
            if old is None or value > old[0]:
                by_slot[slot][category] = (value, equip)
        if not all(by_slot.values()):
            break
        best, best_speed = None, -1
        for combination in product(*(list(by_slot[slot].values()) for slot in plans.SLOTS)):
            sets = Counter(equip.get('Set') for _, equip in combination)
            if require_speed and sets['Speed'] < 4:
                continue
            bonus = sum(sets[key] // stats.tables()['sets'][key]['count'] * value for key, value in bonuses.items())
            speed = base_speed * (1 + bonus) + sum(value for value, _ in combination)
            if speed > best_speed:
                best, best_speed = combination, speed
        if best is None:
            break
        selected = {plans._oid(equip.get('_id')) for _, equip in best}
        results.append(_build('speed', [equip for _, equip in best]))
        remaining = [equip for equip in remaining if plans._oid(equip.get('_id')) not in selected]
    return results


def _frontier(rows):
    """Exact skyline of (x, y, selected items), both coordinates maximized.

    Rows here always have the SAME selected slots and set counts, so their
    completions and set bonuses are identical. Component-wise dominance is
    safe even for the rounded HP/DEF product. Equal points retain the first
    input choice; rebuilding every rank keeps dominated inventory available.
    """
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    out, high = [], float('-inf')
    for row in rows:
        if row[1] > high:
            out.append(row)
            high = row[1]
    return out


def _paired_best(equips, key, names, targets, min_speed=None):
    """Exact multiple-choice DP, with no candidate or search budget limit.

    State = selected slot prefix + set piece counts. Each state keeps every
    nondominated (HP, DEF) or (hit/resist, speed) point, not a top-K sample.
    Only complete reachable set patterns are allowed.
    """
    survival = key == 'survival'
    dims = ('HP', 'Defence') if survival else ('EffectHitRate' if key == 'hit' else 'ResistanceRate', 'Speed')
    base = _reference_base(key)
    by_slot = {slot: {} for slot in plans.SLOTS}
    for equip in equips:
        name = equip.get('Set')
        if name not in names:
            continue
        vec = stats.contribution(base, properties(equip, main=True))
        by_slot[plans.equip_slot(equip)].setdefault(names.index(name), []).append((vec[dims[0]], vec[dims[1]], (equip,)))
    if not all(by_slot.values()):
        return None
    slots = sorted(plans.SLOTS, key=lambda slot: sum(len(v) for v in by_slot[slot].values()))
    pools = [{category: _frontier(rows) for category, rows in by_slot[slot].items()} for slot in slots]
    zero = (0,) * len(names)
    # Suffix maps also give an exact maximum reachable speed for each piece
    # count, so impossible speed constraints never expand a large frontier.
    suffix = [{} for _ in range(7)]
    suffix[6][zero] = 0
    for depth in range(5, -1, -1):
        for counts, value in suffix[depth + 1].items():
            for category, rows in pools[depth].items():
                count = tuple(n + (i == category) for i, n in enumerate(counts))
                maximum = value + max(row[1] for row in rows)
                suffix[depth][count] = max(suffix[depth].get(count, float('-inf')), maximum)
    targets = [target for target in targets if target in suffix[0]]
    if not targets:
        return None

    def reachable(depth, counts, speed):
        for target in targets:
            rest = tuple(a - b for a, b in zip(target, counts))
            maximum = suffix[depth].get(rest)
            if maximum is not None and (survival or int(base['Speed'] + speed + maximum + .5 + 1e-9) >= min_speed):
                return True
        return False

    states = {zero: [(0, 0, ())]}
    for depth, pool in enumerate(pools):
        next_states = {}
        for counts, frontier in states.items():
            for category, items in pool.items():
                count = tuple(n + (i == category) for i, n in enumerate(counts))
                if not reachable(depth + 1, count, max(p[1] for p in frontier) + max(p[1] for p in items)):
                    continue
                rows = next_states.setdefault(count, [])
                for x, y, chosen in frontier:
                    for dx, dy, item in items:
                        ny = y + dy
                        if reachable(depth + 1, count, ny):
                            rows.append((x + dx, ny, chosen + item))
        states = {counts: _frontier(rows) for counts, rows in next_states.items() if rows}
        if not states:
            return None
    best, best_value = None, float('-inf')
    for counts, rows in states.items():
        bonus, _ = stats.set_mods(dict(zip(names, counts)))
        extra = stats.contribution(base, bonus)
        for x, y, chosen in rows:
            if survival:
                hp = int(base['HP'] + x + extra['HP'] + .5 + 1e-9)
                defence = int(base['Defence'] + y + extra['Defence'] + .5 + 1e-9)
                # Integer numerator compares EHP exactly; presentation retains
                # the established auto.metrics formula, HP*(1+DEF/300).
                value = hp * (defence + 300)
            else:
                value = base[dims[0]] + x + extra[dims[0]]
            if value > best_value:
                best, best_value = chosen, value
    if best is None:
        return None
    ordered = sorted(best, key=lambda equip: plans.SLOTS.index(plans.equip_slot(equip)))
    return _build(key, ordered)


def _set_builds(equips, key, limit, min_speed):
    remaining = _ranking_pool(equips)
    results = []
    for _ in range(limit):
        build = None
        if key == 'survival':
            build = _paired_best(remaining, key, ('Health', 'Defense'), [(h, 6-h) for h in (0, 2, 4, 6)])
        else:
            names = ('Hit' if key == 'hit' else 'Resist', 'Health', 'Defense')
            # A lower tier is considered only when the current tier has NO
            # complete speed-feasible build, regardless of its objective.
            for primary in (6, 4, 2):
                targets = [(primary, h, 6-primary-h) for h in range(0, 7-primary, 2)]
                build = _paired_best(remaining, key, names, targets, min_speed)
                if build is not None:
                    break
        if build is None:
            break
        results.append(build)
        selected = set(build['equipIds'])
        remaining = [e for e in remaining if plans._oid(e.get('_id')) not in selected]
    return results


def rankings(equips):
    """Public chart contract. Percent values/panels use 180 for 180%, not 1.8.

    reference is the role name string; referencePanel contains the exact
    supplied naked panel. panel uses stats.STATS keys, sets lists completed
    set bonuses (e.g. ['命中套×2', '生命套×1']), and equipIds follow SLOTS.
    minSpeed=None means unconstrained. Each category consumes its own pool.
    """
    out = []
    for key, label, unit, minimum, limit in (
            ('speed', '速度', '速', None, 3), ('hit', '命中', '%', 200, 3),
            ('resist', '抗性', '%', 150, 3), ('survival', '生存', '分', None, 6)):
        name, reference = RANKING_REFERENCES[key]
        out.append({'key': key, 'label': label, 'unit': unit, 'reference': name,
                    'referencePanel': dict(reference), 'minSpeed': minimum, 'limit': limit,
                    'builds': speed_builds(equips, limit=limit) if key == 'speed'
                    else _set_builds(equips, key, limit, minimum)})
    return out


def overview(equips):
    rated = [(equip, quality(equip)) for equip in equips]
    maxed = [(equip, row) for equip, row in rated if (equip.get('LV') or 0) >= 15]
    axes = [
        {'label': '一速', 'value': max((row['speed'] for _, row in maxed), default=0), 'target': 30},
        {'label': '高速', 'value': sum(row['speed'] >= 16 for _, row in maxed), 'target': 20},
        {'label': '输出', 'value': sum(row['score'] >= 48 and row['offense'] >= 30 for _, row in maxed), 'target': 80},
        {'label': '肉装', 'value': sum(row['score'] >= 48 and row['tank'] >= 30 for _, row in maxed), 'target': 80},
        {'label': '双效', 'value': sum(row['dual'] >= 30 for _, row in maxed), 'target': 40},
        {'label': '半肉', 'value': sum(row['score'] >= 48 and row['offense'] >= 20 and row['tank'] >= 20 for _, row in maxed), 'target': 80},
    ]
    return {'updatedAt': int(time.time() * 1000), 'total': len(equips), 'maxed': len(maxed),
            'qualified': sum(row['score'] >= 48 for _, row in maxed),
            'small': sum(row['score'] >= 57 for _, row in maxed), 'large': sum(row['score'] >= 62 for _, row in maxed),
            'embryos': sum((equip.get('LV') or 0) < 15 and (row['score'] >= 20 or row['speed'] >= 4) for equip, row in rated),
            'speedCounts': [{'threshold': n, 'speedSet': sum(row['speed'] >= n and equip.get('Set') == 'Speed' for equip, row in maxed),
                             'all': sum(row['speed'] >= n for _, row in maxed)} for n in (16, 18, 20)],
            'speedBands': [
                {'label': '14-15', 'min': 14, 'max': 15, 'count': sum(14 <= row['speed'] <= 15 for _, row in maxed)},
                {'label': '16-18', 'min': 16, 'max': 18, 'count': sum(16 <= row['speed'] <= 18 for _, row in maxed)},
                {'label': '19-20', 'min': 19, 'max': 20, 'count': sum(19 <= row['speed'] <= 20 for _, row in maxed)},
                {'label': '21+', 'min': 21, 'max': None, 'count': sum(row['speed'] >= 21 for _, row in maxed)},
            ],
            'axes': axes, 'power': round(sum(min(1, axis['value'] / axis['target']) for axis in axes) / 6 * 100, 1),
            'speedBuilds': speed_builds(equips), 'speedSetBuilds': speed_builds(equips, require_speed=True),
            'rankings': rankings(equips),
            'reference': '兔女郎爱莉卡 · Lv60 满觉醒 · 无羁绊'}
