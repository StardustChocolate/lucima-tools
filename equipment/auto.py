"""多角色装备搜索：硬性属性条件、输出/生存评分与第二类优先。

搜索逐部位分支定界，所有可行性上界包含额外套装加成。
有属性上限时按可达区间收紧评分上界，避免区间外高评分分支耗尽预算。
同一显示速度只保留主评分最高的方案。完整模式不设节点或结果数量上限。
"""
from __future__ import annotations

import heapq
import itertools
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import lru_cache
from . import stats
from .plans import SLOTS, equip_slot, worn_items
from backend.tasks import _aux_score

STATS = stats.STATS
IDX = {s: i for i, s in enumerate(STATS)}
MAX_PIECES = 6
NODE_LIMIT = 300_000
DEFAULT_TOLERANCE = 3.0
DEFAULT_RESULT_LIMIT = 200
MAX_RESULT_LIMIT = 1000
NEIGHBOR_LIMIT = 75_000
SECONDARY = ("Speed", "ResistanceRate", "EffectHitRate")


class BuildError(ValueError):
    pass


class SearchCancelled(BuildError):
    pass


def _oid(v):
    return v.get("$oid") if isinstance(v, dict) else v


def number(v, label):
    try:
        value = float(v)
    except (TypeError, ValueError):
        raise BuildError(f"{label}必须是数字") from None
    if not math.isfinite(value) or value < 0:
        raise BuildError(f"{label}必须是非负有限数值")
    return value


def normalize_sets(names):
    if not isinstance(names or [], list):
        raise BuildError("套装必须是列表")
    need, fours = {}, 0
    for name in names or []:
        info = stats.tables()["sets"].get(str(name))
        if not info:
            raise BuildError(f"未知套装 {name}")
        fours += info["count"] == 4
        need[name] = need.get(name, 0) + info["count"]
    if fours > 1 or sum(need.values()) > 6:
        raise BuildError("最多 1 个四件套，套装总件数不能超过 6 件")
    return need


def selected_set_groups(names):
    """GUI alternatives: one 4+2, or three 2-piece sets (immunity fixed once)."""
    if not isinstance(names, list):raise BuildError('套装筛选必须是列表')
    table=stats.tables()['sets']
    if any(not isinstance(name,str) or name not in table for name in names):raise BuildError('套装筛选包含未知套装')
    names=list(dict.fromkeys(names))
    if not names:return [{}]
    fours=[name for name in names if table[name]['count']==4]
    twos=[name for name in names if table[name]['count']==2]
    if fours:
        return [{four:4,two:2} for four in fours for two in (twos or [name for name,row in table.items() if row['count']==2])]
    fixed=['Immunity'] if 'Immunity' in twos else []
    choices=[name for name in twos if name not in fixed]
    if not choices:choices=[name for name,row in table.items() if row['count']==2 and name!='Immunity']
    result=[]
    for combo in itertools.combinations_with_replacement(choices,3-len(fixed)):
        counts={}
        for name in [*fixed,*combo]:counts[name]=counts.get(name,0)+2
        result.append(counts)
    return result


def normalize_conditions(conds):
    if not isinstance(conds or [], list):
        raise BuildError("属性条件必须是列表")
    out = []
    for c in conds or []:
        if not isinstance(c, dict) or c.get("stat") not in IDX:
            raise BuildError("未知属性条件")
        stat = c["stat"]
        lo = None if c.get("min") in (None, "") else number(c["min"], "下限")
        hi = None if c.get("max") in (None, "") else number(c["max"], "上限")
        if lo is not None and hi is not None and lo > hi:
            raise BuildError(f"{stats.STAT_CN[stat]}的下限大于上限")
        if any(x["stat"] == stat for x in out):
            raise BuildError(f"{stats.STAT_CN[stat]}重复设置了条件")
        out.append({"stat": stat, "min": lo, "max": hi})
    return out


def ranking_config(critical_target=100, objective="damage", secondary=None):
    target = number(critical_target, "暴击率") if objective == "damage" else 0
    if target > 100:
        raise BuildError("暴击率应在 0～100 之间")
    if objective not in ("damage", "survival"):
        raise BuildError("请选择输出或生存排序")
    if not isinstance(secondary or [], list) or any(s not in SECONDARY for s in secondary or []):
        raise BuildError("第二类只支持速度、命中、抗性")
    return target, objective, list(dict.fromkeys(secondary or []))


def metrics(panel, target):
    crit = 1.0 if target == 0 else max(0, min(1, panel["CriticalRate"] / target))
    cd = max(0, min(350, panel["CriticalDamageRate"])) / 100
    return {"damage": panel["Attack"] * crit * cd,
            "survival": panel["HP"] * (1 + panel["Defence"] / 300),
            "effectiveCrit": crit * 100, "effectiveCritDamage": min(350, max(0, panel["CriticalDamageRate"]))}


def candidate_pool(state, role, *, only_max, include_worn, free_roles=None, exclude=None, excluded_roles=None):
    free_roles = set(free_roles or ()) | {_oid(role.get("_id"))}
    exclude = set(exclude or ())
    excluded_roles = set(excluded_roles or ())
    out = []
    seen = set()
    unknown = set()
    for eq in (state.get("EquipmentContainer") or {}).get("Datas") or []:
        eid = _oid(eq.get("_id"))
        if not eid or eid in exclude or eid in seen:
            continue
        if only_max and int(eq.get("LV") or 0) < 15:
            continue
        owner = _oid(eq.get("EquipRole")) or ""
        # 本轮保护角色优先于“包含其他角色”和本轮所选角色的装备开放规则。
        if owner and owner in excluded_roles:
            continue
        if owner and owner not in free_roles and not include_worn:
            continue
        if equip_slot(eq) not in SLOTS:
            unknown.add(str(eq.get("StaticID") or eid))
            continue
        seen.add(eid)
        out.append(eq)
    if unknown:
        raise BuildError("可用装备中存在无法识别部位的物品：" + "、".join(sorted(unknown))
                         + "。请补齐装备资料后重新计算，避免漏选可行方案。")
    return out


def _patterns(need):
    labels = [name for name, count in need.items() for _ in range(count)]
    labels += [None] * (6 - len(labels))
    return sorted(set(itertools.permutations(labels)), key=lambda p: tuple(x or "" for x in p))


def _set_bonus_vec(base, counts):
    mods, _ = stats.set_mods(counts)
    values = stats.contribution(base, mods)
    return [values[s] for s in STATS]


def _free_slot_bounds(base, free, need, available=None):
    # 背包可行性上界：允许自由位补齐任何套装，包括重复两件套。
    n = len(STATS)
    hi, lo = [[0.] * n for _ in range(free + 1)], [[0.] * n for _ in range(free + 1)]
    for name, info in stats.tables()["sets"].items():
        if available is not None and name not in available:
            continue
        step = _set_bonus_vec(base, {name: info["count"]})
        old = need.get(name, 0)
        nh, nl = [v[:] for v in hi], [v[:] for v in lo]
        for used in range(free + 1):
            for add in range(1, used + 1):
                times = (old + add) // info["count"] - old // info["count"]
                for i in range(n):
                    nh[used][i] = max(nh[used][i], hi[used-add][i] + step[i] * times)
                    nl[used][i] = min(nl[used][i], lo[used-add][i] + step[i] * times)
        hi, lo = nh, nl
    return hi[free], lo[free]


def _bucket(panel, secondary):
    return tuple(panel[s] for s in secondary)


def _rank_key(combo, objective, secondary, target):
    p = combo["panel"]
    # 不让无收益暴击/爆伤通过“副分”重新获得排序优势。
    key = (*(p[s] for s in secondary), combo["metrics"][objective])
    return key if objective == "survival" else (*key, -max(0, p["CriticalRate"] - target), -max(0, p["CriticalDamageRate"] - 350))


class _TopCandidates:
    """每个显示速度保留主评分最高的一套，再按用户排序展示不同速度。

    主评分更高的组内代表可能命中/抗性更低，因此保留全部已见速度组，
    不按全局展示排名提前逐出速度组，避免后续代表变化使剪枝失效。
    """
    def __init__(self, limit, objective, secondary, target):
        self.limit, self.objective, self.secondary, self.target = limit, objective, secondary, target
        self.heap, self.ids, self.by_speed = [], set(), {}
        self.serial = itertools.count()
        self.limited = False

    def offer(self, combo):
        ids = tuple(combo["equipIds"][slot] for slot in SLOTS)
        if ids in self.ids: return
        key = _rank_key(combo, self.objective, self.secondary, self.target)
        speed = combo["panel"]["Speed"]
        old = self.by_speed.get(speed)
        # 同速先看第一类评分，第二类仅用于主评分相同的稳定择优。
        if old is not None:
            if (combo["metrics"][self.objective], *key) <= (old[3]["metrics"][self.objective], *old[0]): return
            self.heap.remove(old)
            heapq.heapify(self.heap)
            self.ids.discard(old[2])
        entry = (key, -next(self.serial), ids, combo)
        heapq.heappush(self.heap, entry)
        self.by_speed[speed] = entry
        self.ids.add(ids)
        self.limited = self.limit is not None and len(self.heap) > self.limit

    def skips(self, upper_key, speed_low=None, speed_high=None):
        # 只有这个分支可能达到的每个速度均已有不劣的组内代表，才能剪枝。
        # 满展示上限不能用来剪掉尚未发现的速度，尤其二类含命中/抗性时。
        if speed_low is None or speed_high is None: return False
        lo, hi = math.ceil(speed_low), math.floor(speed_high)
        if hi - lo + 1 > len(self.by_speed): return False
        group_upper = (upper_key[len(self.secondary)], *upper_key)
        for speed in range(lo, hi+1):
            old = self.by_speed.get(speed)
            if old is None or (old[3]["metrics"][self.objective], *old[0]) < group_upper: return False
        return True

    def results(self):
        return [entry[3] for entry in sorted(self.heap, key=lambda entry: (entry[0], entry[1]), reverse=True)[:self.limit]]


@dataclass(slots=True)
class _ChoiceRange:
    high: tuple
    low: tuple
    set_name: str | None
    left: object = None
    right: object = None
    item: dict | None = None
    joint: tuple = ()
    window: tuple = ()


def _joint_frontier(points, limit=256):
    """两项下限的可达性边界；过大时向上放宽合并，绝不删除潜在可行分支。"""
    frontier, best_y = [], -math.inf
    for x, y in sorted(set(points), reverse=True):
        if y > best_y:
            frontier.append((x, y))
            best_y = y
    if len(frontier) > limit:
        step = math.ceil(len(frontier) / limit)
        frontier = [(frontier[i][0], frontier[min(i+step, len(frontier))-1][1])
                    for i in range(0, len(frontier), step)]
    return tuple(frontier)


def _window_profile(rows, limit=128):
    """保留有上下限属性的可达区间及其他属性上界，不跨速度做支配删除。

    压缩只向外扩大区间、向上放宽属性值；可以多搜，不能误删真解。
    """
    merged = []
    for lo, hi, values in sorted(rows, key=lambda row: (row[0], row[1])):
        if merged and merged[-1][:2] == (lo, hi):
            old = merged.pop()[2]
            values = tuple(max(a, b) for a, b in zip(old, values))
        merged.append((lo, hi, values))
    if len(merged) > limit:
        step = math.ceil(len(merged) / limit)
        groups = []
        for i in range(0, len(merged), step):
            chunk = merged[i:i+step]
            groups.append((chunk[0][0], max(row[1] for row in chunk),
                           tuple(max(row[2][j] for row in chunk) for j in range(len(STATS)))))
        merged = groups
    return tuple(merged)


class _WindowLookup:
    """按可达区间查询属性上界；线段树避免每个搜索节点扫描全部后缀。"""
    def __init__(self, rows):
        self.lows = tuple(row[0] for row in rows)
        highs, high = [], -math.inf
        for _, hi, _ in rows:
            high = max(high, hi)
            highs.append(high)
        self.highs = tuple(highs)
        self.size = 1 << (len(rows) - 1).bit_length()
        self.tree = [None] * (2 * self.size)
        for i, row in enumerate(rows): self.tree[self.size+i] = row[2]
        for i in range(self.size-1, 0, -1):
            self.tree[i] = self.merge(self.tree[2*i], self.tree[2*i+1])

    @staticmethod
    def merge(a, b):
        if a is None: return b
        if b is None: return a
        return tuple(max(x, y) for x, y in zip(a, b))

    def query(self, lo, hi):
        left = bisect_left(self.highs, lo - 1e-9) + self.size
        right = bisect_right(self.lows, hi + 1e-9) + self.size
        found = None
        while left < right:
            if left & 1:
                found = self.merge(found, self.tree[left]); left += 1
            if right & 1:
                right -= 1; found = self.merge(found, self.tree[right])
            left //= 2; right //= 2
        return found


def _explore_neighbors(seed, pool, by_slot, fixed, candidate, top, budget, cancelled=None):
    """先以完整可行配装探索一/两部位替换，避免首套后只追逐松散上界。

    所有候选仍经过相同硬条件校验；这里只提供候选，不作全局最优证明。
    每对部位检查完就采用更优方案，评估次数与主搜索共享总预算。
    """
    current = [pool[seed["equipIds"][slot]] for slot in SLOTS]
    key = _rank_key(seed, top.objective, top.secondary, top.target)
    used = 0
    while used < budget:
        changed = False
        for i, j in itertools.combinations(range(6), 2):
            rest = [it for k, it in enumerate(current) if k not in (i, j)]
            rest_vec = tuple(fixed[k] + sum(it["vec"][k] for it in rest) for k in range(len(STATS)))
            rest_counts = {}
            for it in rest: rest_counts[it["set"]] = rest_counts.get(it["set"], 0) + 1
            pivot = current
            for a in by_slot[SLOTS[i]]:
                for b in by_slot[SLOTS[j]]:
                    used += 1
                    if used % 128 == 0 and cancelled and cancelled():
                        raise SearchCancelled("已停止配装计算，未返回未完成的方案")
                    counts = dict(rest_counts)
                    for it in (a, b): counts[it["set"]] = counts.get(it["set"], 0) + 1
                    total = tuple(x+y+z for x, y, z in zip(rest_vec, a["vec"], b["vec"]))
                    picked = list(current)
                    picked[i], picked[j] = a, b
                    combo = candidate(picked, total, counts)
                    if combo is not None:
                        top.offer(combo)
                        new_key = _rank_key(combo, top.objective, top.secondary, top.target)
                        if new_key > key:
                            pivot, key = picked, new_key
                    if used >= budget: break
                if used >= budget: break
            if pivot is not current:
                current, changed = pivot, True
            if used >= budget: break
        if not changed: break
    return used


def _search_combinations(base, fixed, by_slot, need, conds, secondary, objective, target, limit, current,
                         *, complete=False, progress=None, cancelled=None, set_groups=None):
    """全套装分支共用优先队列，以第二类上界优先，再比较主评分上界。

    按候选区间二分延迟展开，避免大背包一次创建所有子节点。
    完整模式用深度优先顺序控制内存，直到所有分支检查或安全剪枝后才返回。
    有限模式保留给旧调用及离线预算回归，不用于装备界面的配装请求。
    """
    n = len(STATS)
    groups=set_groups if set_groups is not None else [need]
    available = {it["set"] for items in by_slot.values() for it in items}
    top = _TopCandidates(limit, objective, secondary, target)
    queue, contexts = [], []
    seeding = True
    serial = itertools.count()
    best_possible = {c["stat"]: None for c in conds}
    minimums = [c for c in conds if c["min"] is not None]
    # 暴击目标与速度等条件必须同时能达到，不能只看彼此独立的最大值。
    minimums.sort(key=lambda c: c["stat"] != "CriticalRate")
    joint_stats = tuple(IDX[c["stat"]] for c in minimums[:2]) if len(minimums) >= 2 else ()
    joint_targets = tuple(c["min"] / (100 if c["stat"] in stats.PERCENT_STATS else 1)
                          - (.00050001 if c["stat"] in stats.PERCENT_STATS else .50000001)
                          for c in minimums[:2]) if joint_stats else ()
    windows = sorted((c for c in conds if c["max"] is not None), key=lambda c: c["stat"] != "Speed")
    window_condition = windows[0] if windows else None
    window_stat = IDX[window_condition["stat"]] if windows else None
    if windows:
        scale = 100 if window_condition["stat"] in stats.PERCENT_STATS else 1
        epsilon = .00050001 if scale == 100 else .50000001
        window_min = -math.inf if window_condition["min"] is None else window_condition["min"] / scale - epsilon
        window_max = window_condition["max"] / scale + epsilon

    @lru_cache(maxsize=20000)
    def actual_bonus(counts):
        return _set_bonus_vec(base, dict(counts))

    def candidate(picked, total=None, counts=None):
        if total is None:
            counts, total = {}, list(fixed)
            for item in picked:
                counts[item["set"]] = counts.get(item["set"], 0) + 1
                total = [a+b for a, b in zip(total, item["vec"])]
        if not any(all(counts.get(name,0)>=count for name,count in group.items()) for group in groups):return None
        bonus = actual_bonus(tuple(sorted(counts.items())))
        constrained = stats.display({c["stat"]: total[IDX[c["stat"]]] + bonus[IDX[c["stat"]]] for c in conds})
        if any((c["min"] is not None and constrained[c["stat"]] < c["min"]) or
               (c["max"] is not None and constrained[c["stat"]] > c["max"]) for c in conds): return None
        shown = stats.display({s: total[i] + bonus[i] for i, s in enumerate(STATS)})
        return {"equipIds": {it["slot"]: it["id"] for it in picked}, "panel": shown,
                "aux": sum(it["aux"] for it in picked), "sets": stats.set_mods(counts)[1],
                "metrics": metrics(shown, target)}

    # 已穿且属于本轮可用池的完整方案也参与候选，避免已知可行方案被预算漏掉。
    pool = {it["id"]: it for items in by_slot.values() for it in items}
    worn_ids = [_oid(eq.get("_id")) for eq in current]
    if len(worn_ids) == 6 and all(eid in pool for eid in worn_ids):
        seed = candidate([pool[eid] for eid in worn_ids])
        if seed is not None:
            top.offer(seed)
            seeding = False

    @lru_cache(maxsize=20000)
    def bonuses(free, counts):
        counts = dict(counts)
        actual = _set_bonus_vec(base, counts)
        high, low = _free_slot_bounds(base, free, counts, available)
        return tuple(a + h for a, h in zip(actual, high)), tuple(a + l for a, l in zip(actual, low))

    @lru_cache(maxsize=None)
    def choices(slot, label):
        items = [it for it in by_slot[slot] if label is None or it["set"] == label]
        if not items:
            return None, 0
        items.sort(key=lambda it: (*(-it["vec"][IDX[s]] for s in secondary),
                   -(it["aux"] if objective == "damage" else it["vec"][IDX["HP"]] + it["vec"][IDX["Defence"]]), it["id"]))

        def build(lo, hi):
            if hi - lo == 1:
                it = items[lo]
                joint = (tuple(it["vec"][i] for i in joint_stats),) if joint_stats else ()
                window = ((it["vec"][window_stat], it["vec"][window_stat], tuple(it["vec"])),) if windows else ()
                return _ChoiceRange(tuple(it["vec"]), tuple(it["vec"]), it["set"], item=it, joint=joint, window=window)
            mid = (lo + hi) // 2
            left, right = build(lo, mid), build(mid, hi)
            return _ChoiceRange(tuple(max(a, b) for a, b in zip(left.high, right.high)),
                                tuple(min(a, b) for a, b in zip(left.low, right.low)),
                                left.set_name if left.set_name == right.set_name else None, left, right,
                                joint=_joint_frontier((*left.joint, *right.joint)) if joint_stats else (),
                                window=_window_profile((*left.window, *right.window)) if windows else ())
        return build(0, len(items)), len(items)

    def bounds(ctx, k, cur, counts, branch):
        roots, labels, sh, sl, free, joint_suffix, window_suffix = ctx
        counts = dict(counts)
        remaining = free[k]
        # 当前候选区间若都属同套装，先计入该件，收紧额外套装上界。
        if labels[k] is None and branch.set_name is not None:
            counts[branch.set_name] = counts.get(branch.set_name, 0) + 1
            remaining -= 1
        high_bonus, low_bonus = bonuses(remaining, tuple(sorted(counts.items())))
        upper = stats.display({s: cur[i] + branch.high[i] + sh[k+1][i] + high_bonus[i] for i, s in enumerate(STATS)})
        lower = stats.display({s: cur[i] + branch.low[i] + sl[k+1][i] + low_bonus[i] for i, s in enumerate(STATS)})
        # 找首个可行方案时，兼顾所有尚未达到的最低条件，避免只堆攻击。
        coverage = []
        for c in conds:
            if c["min"] is None: continue
            i = IDX[c["stat"]]
            goal = c["min"] / (100 if c["stat"] in stats.PERCENT_STATS else 1)
            if goal > fixed[i]:
                progress = (cur[i] + branch.high[i] + low_bonus[i] - fixed[i]) / (goal - fixed[i])
                coverage.append(min(1, progress))
        joint_ok = True
        if joint_stats:
            x, y = joint_stats
            need_x = joint_targets[0] - cur[x] - high_bonus[x]
            need_y = joint_targets[1] - cur[y] - high_bonus[y]
            negatives, heights = joint_suffix[k+1]
            joint_ok = False
            for px, py in branch.joint:
                end = bisect_right(negatives, -(need_x - px) + 1e-9)
                if end and heights[end-1] + py >= need_y - 1e-9:
                    joint_ok = True
                    break
        if joint_ok and windows:
            # 评分上界也只能来自速度区间可达的组合，不能用区间外的高攻击
            # 或高爆伤挤占预算。所有最低条件仍一起检查（含硬性暴击目标）。
            feasible_high = None
            lo = window_min - cur[window_stat] - high_bonus[window_stat]
            hi = window_max - cur[window_stat] - low_bonus[window_stat]
            for branch_lo, branch_hi, values in branch.window:
                suffix = window_suffix[k+1].query(lo - branch_hi, hi - branch_lo)
                if suffix is None: continue
                possible = stats.display({s: cur[i] + values[i] + suffix[i] + high_bonus[i] for i, s in enumerate(STATS)})
                if any(c["min"] is not None and possible[c["stat"]] < c["min"] for c in conds): continue
                feasible_high = possible if feasible_high is None else {s: max(feasible_high[s], possible[s]) for s in STATS}
            joint_ok = feasible_high is not None
            if joint_ok:
                upper = {s: min(upper[s], feasible_high[s]) for s in STATS}
        # 有上限的属性不可能在最终方案中超过该值，也不应抬高排序上界。
        for c in windows:
            upper[c["stat"]] = min(upper[c["stat"]], c["max"])
        return upper, lower, min(coverage, default=0), joint_ok

    def push(ci, k, cur, counts, chosen, branch, depth):
        upper, lower, coverage, joint_ok = bounds(contexts[ci], k, cur, counts, branch)
        if not joint_ok: return
        for c in conds:
            if c["min"] is not None and upper[c["stat"]] < c["min"]: return
            if c["max"] is not None and lower[c["stat"]] > c["max"]: return
        score = metrics(upper, target)[objective]
        bucket = _bucket(upper, secondary)
        key = (*bucket, score)
        if objective == "damage":
            key += (-max(0, lower["CriticalRate"] - target), -max(0, lower["CriticalDamageRate"] - 350))
        span = (lower["Speed"], upper["Speed"])
        if top.skips(key, *span): return
        rank = tuple(-x for x in key)
        # 先在最高第二类上界内深入找到完整方案，再优化同档主评分。
        # 否则互不兼容的攻击/暴击/爆伤上界可能在首个完整方案前耗尽预算。
        priority = (-depth, rank) if complete else ((rank[:len(secondary)], -coverage, -depth, rank[len(secondary):]) if seeding else rank)
        heapq.heappush(queue, (priority, next(serial), rank, depth, ci, k, cur, counts, chosen, branch, span))

    patterns=dict.fromkeys(pattern for group in groups for pattern in _patterns(group))
    for pattern in patterns:
        if cancelled and cancelled(): raise SearchCancelled("已停止配装计算，未返回未完成的方案")
        options = [(choices(s, label), label) for s, label in zip(SLOTS, pattern)]
        if any(root is None for (root, _), _ in options): continue
        # 先确定第二类属性波动大的部位，避免把“最高速度”和另一件的
        # “最高输出”拼成不可实现的上界，挤占完整方案的搜索预算。
        options.sort(key=lambda entry: (*(-(entry[0][0].high[IDX[s]] - entry[0][0].low[IDX[s]]) for s in secondary), entry[0][1]))
        roots = [entry[0][0] for entry in options]
        labels = [entry[1] for entry in options]
        sh, sl = [[0.] * n for _ in range(7)], [[0.] * n for _ in range(7)]
        free = [0] * 7
        for k in range(5, -1, -1):
            sh[k] = [a+b for a, b in zip(roots[k].high, sh[k+1])]
            sl[k] = [a+b for a, b in zip(roots[k].low, sl[k+1])]
            free[k] = free[k+1] + (labels[k] is None)
        joint_suffix = [None] * 7
        if joint_stats:
            frontier = ((0., 0.),)
            joint_suffix[6] = ((0.,), (0.,))
            for k in range(5, -1, -1):
                frontier = _joint_frontier((a+x, b+y) for a, b in roots[k].joint for x, y in frontier)
                joint_suffix[k] = (tuple(-x for x, _ in frontier), tuple(y for _, y in frontier))
        window_suffix = [None] * 7
        if windows:
            profile = ((0., 0., (0.,) * n),)
            window_suffix[6] = _WindowLookup(profile)
            for k in range(5, -1, -1):
                profile = _window_profile((lo+a, hi+b, tuple(x+y for x, y in zip(values, other)))
                                          for lo, hi, values in roots[k].window for a, b, other in profile)
                window_suffix[k] = _WindowLookup(profile)
        ctx = (roots, labels, sh, sl, free, joint_suffix, window_suffix)
        ci = len(contexts)
        contexts.append(ctx)
        counts = tuple(sorted((name,pattern.count(name)) for name in set(pattern) if name is not None))
        upper, _, _, _ = bounds(ctx, 0, tuple(fixed), counts, roots[0])
        for c in conds:
            name = c["stat"]
            value = upper[name] / 100 if name in stats.PERCENT_STATS else upper[name]
            best_possible[name] = max(best_possible[name] or 0, value)
        push(ci, 0, tuple(fixed), counts, (), roots[0], 0)

    nodes = neighbor_nodes = 0
    explore = math.prod(len(items) for items in by_slot.values()) > NODE_LIMIT
    while queue and (complete or nodes < NODE_LIMIT):
        if nodes % 1024 == 0:
            if cancelled and cancelled(): raise SearchCancelled("已停止配装计算，未返回未完成的方案")
            if progress: progress({"nodes":nodes,"speedCount":len(top.heap),"pending":len(queue)})
        if explore and top.heap:
            explore = False
            warm_budget = NEIGHBOR_LIMIT if complete else min(NEIGHBOR_LIMIT, NODE_LIMIT // 4, NODE_LIMIT - nodes)
            neighbor_nodes = _explore_neighbors(top.results()[0], pool, by_slot, fixed, candidate, top,
                                                warm_budget, cancelled)
            nodes += neighbor_nodes
            if not complete and nodes >= NODE_LIMIT: break
        _, _, rank, depth, ci, k, cur, counts, chosen, branch, span = heapq.heappop(queue)
        nodes += 1
        if top.skips(tuple(-x for x in rank), *span): continue
        depth += 1
        if branch.item is None:
            push(ci, k, cur, counts, chosen, branch.left, depth)
            push(ci, k, cur, counts, chosen, branch.right, depth)
            continue
        item = branch.item
        nxt = tuple(a+b for a, b in zip(cur, item["vec"]))
        picked = (*chosen, item)
        roots, labels, _, _, _, _, _ = contexts[ci]
        if labels[k] is None:
            counts = dict(counts)
            counts[item["set"]] = counts.get(item["set"], 0) + 1
            counts = tuple(sorted(counts.items()))
        if k < 5:
            push(ci, k+1, nxt, counts, picked, roots[k+1], depth)
            continue
        combo = candidate(picked)
        if combo is None: continue
        top.offer(combo)
        if seeding:
            seeding = False
            if not complete:
                queue = [(entry[2], *entry[1:]) for entry in queue]
                heapq.heapify(queue)
    if cancelled and cancelled(): raise SearchCancelled("已停止配装计算，未返回未完成的方案")
    best = max((entry[0] for entry in top.heap), default=None)
    best_confirmed = best is not None and (not queue or (not complete and best >= tuple(-x for x in queue[0][2])))
    if queue and any(s != "Speed" for s in secondary):
        # 组内输出/生存提高时，命中/抗性代表值可能下降；队列未清空时
        # 不能用当前展示首选的排名证明最终首选不再变化。
        best_confirmed = False
    if progress: progress({"nodes":nodes,"speedCount":len(top.heap),"pending":len(queue)})
    return {"results": top.results(), "nodes": nodes, "neighborNodes": neighbor_nodes,
            "truncated": bool(queue) and not complete and nodes >= NODE_LIMIT,
            "searchComplete": not queue and not top.limited,
            "candidateLimit": limit, "moreCandidatesPossible": bool(queue) or top.limited,
            "bestPossible": best_possible, "bestConfirmed": best_confirmed}


def search(state, role, *, sets=None, set_options=None, conditions=None, only_max=True, include_worn=False,
           free_roles=None, exclude=None, excluded_roles=None, limit=DEFAULT_RESULT_LIMIT, critical_target=100, objective="damage", secondary=None, secondary_min=None, tolerance=DEFAULT_TOLERANCE,
           complete=False, progress=None, cancelled=None):
    need, conds = normalize_sets(sets), normalize_conditions(conditions)
    groups=selected_set_groups(set_options) if set_options is not None else None
    target, objective, secondary = ranking_config(critical_target, objective, secondary)
    # 输出目标和显式暴击区间同时生效；生存模式仅按显式区间限制暴击。
    if objective == "damage" and target > 0:
        critical = next((c for c in conds if c["stat"] == "CriticalRate"), None)
        if critical is None:
            conds.append({"stat": "CriticalRate", "min": target, "max": None})
        else:
            critical["min"] = max(target, critical["min"] or 0)
    limit = None if complete else max(1, min(int(limit), MAX_RESULT_LIMIT))
    tolerance = number(tolerance, "评分容差")
    if tolerance > 100: raise BuildError("评分容差应在 0～100% 之间")
    # 第二类只参与排序；属性门槛统一从“筛选条件”读取。
    # 保留旧参数以兼容旧客户端，但不再将旧 secondaryMin 当成筛选条件。
    base = stats.role_base(role)
    if not base:
        raise BuildError("缺少该角色的基础属性资料")
    role_mods = stats.role_mods(state, role, include_artifact_effects=True)
    fixed = stats.panel(base, role_mods)
    fixed_vec = [fixed[s] for s in STATS]
    pool = candidate_pool(state, role, only_max=only_max, include_worn=include_worn,
                          free_roles=free_roles, exclude=exclude, excluded_roles=excluded_roles)
    by_slot = {s: [] for s in SLOTS}
    for eq in pool:
        contrib = stats.contribution(base, stats.equip_mods(eq), role_mods)
        by_slot[equip_slot(eq)].append({"id": _oid(eq.get("_id")), "slot": equip_slot(eq),
            "set": eq.get("Set") or "", "vec": [contrib[s] for s in STATS],
            "aux": _aux_score((eq.get("SubProps") or {}).get("SourceValues") or [])})
    current, _ = worn_items((state.get("EquipmentContainer") or {}).get("Datas") or [], _oid(role.get("_id")))
    pool_counts = {s:len(items) for s,items in by_slot.items()}
    if complete:
        # 相同部位、套装和全部属性的装备只需一个代表；后续角色仍从排除
        # 已选ID后的装备池重算，因此不会丢掉其他角色可用的同属性装备。
        worn_ids = {_oid(eq.get("_id")) for eq in current}
        for slot, items in by_slot.items():
            unique = {}
            for item in sorted(items,key=lambda item:item["id"] not in worn_ids):
                unique.setdefault((item["set"],tuple(item["vec"])),item)
            by_slot[slot] = list(unique.values())
    set_base = {stat: value * (1 + role_mods.get(stat + "FinalRate", 0)) for stat, value in base.items()}
    found = _search_combinations(set_base, fixed_vec, by_slot, need, conds, secondary, objective, target, limit, current,
                                 complete=complete,progress=progress,cancelled=cancelled,set_groups=groups)
    for combo in found["results"]:
        primary_name = "输出评分" if objective == "damage" else "生存评分"
        order = "不同速度按第二类属性优先，同档比较主评分" if secondary else "不同速度按主评分从高到低排序"
        combo["reason"] = f"同速度保留{primary_name}最高方案；{order}"
        combo["secondaryStatus"] = {s: {"actual": combo["panel"][s]} for s in secondary}
    best_possible = found.pop("bestPossible")
    shortage={}
    for name,cnt in need.items():
        per={s:sum(it["set"]==name for it in by_slot[s]) for s in SLOTS}
        if sum(v>0 for v in per.values())<cnt: shortage[name]={"need":cnt,"slots":per}
    return {**found, "shortage": shortage,
            "pool": pool_counts, "current": stats.role_panel(state, role, current, include_artifact_effects=True),
            "base": stats.display(fixed), "criticalTarget": target, "objective": objective, "secondary": secondary,
            "bestPossible": {k: stats.display({k: v})[k] if v is not None else None for k, v in best_possible.items()}}



def search_multi(state,roles,entries,*,only_max=True,include_worn=False,limit=DEFAULT_RESULT_LIMIT,prefix=None,
                 excluded_roles=None,complete=False,progress=None,cancelled=None):
    """前缀为服务器保存的已选组合；改选只重算后缀，不再搜索前面角色。"""
    if not entries or len(entries)>6 or len({e.get("roleId") for e in entries}) != len(entries):
        raise BuildError("请选择 1～6 个不重复的角色")
    free_roles={e.get("roleId") for e in entries}
    out=list(prefix or [])
    reserved=set()
    for r in out:
        if r.get("results"):
            reserved.update(r["results"][r["pick"]]["equipIds"].values())
        if r.get("done"):
            pass  # 只保留最终选中装备；旧快照中被替换下来的装备仍可再分配。
    for index,entry in enumerate(entries[len(out):],len(out)):
        if cancelled and cancelled(): raise SearchCancelled("已停止配装计算，未返回未完成的方案")
        def report(value):
            if progress: progress({**value,"roleIndex":index+1,"roleCount":len(entries)})
        report({"nodes":0,"speedCount":0,"pending":0})
        rid=entry.get("roleId")
        role=roles.get(rid)
        if role is None: raise BuildError("角色不存在，请同步账号后重新选择")
        try:
            mode = entry.get('mode', 'conditions')
            if mode not in ('conditions', 'advisor'):
                raise BuildError('请选择按条件配装或自动推荐配装')
            effective_exclusions = entry.get("excludedRoleIds", excluded_roles) if include_worn else []
            if mode == 'advisor':
                from . import recommend
                res = recommend.search(state, role, profile_id=entry.get('profileId'), depth=entry.get('depth', 'standard'), hit_target=entry.get('hitTarget',100),
                    sets=entry.get('sets'), set_options=entry.get('setOptions'), conditions=entry.get('conditions'), only_max=only_max, include_worn=include_worn,
                    free_roles=free_roles, exclude=reserved, excluded_roles=effective_exclusions, progress=report, cancelled=cancelled)
            else:
                res=search(state,role,sets=entry.get("sets"),set_options=entry.get('setOptions'),conditions=entry.get("conditions"),
                    only_max=only_max,include_worn=include_worn,free_roles=free_roles,exclude=reserved,
                    excluded_roles=effective_exclusions,limit=limit,
                    critical_target=entry.get("criticalTarget",100),objective=entry.get("objective","damage"),secondary=entry.get("secondary"),secondary_min=entry.get("secondaryMin"),tolerance=entry.get("tolerance",DEFAULT_TOLERANCE),
                    complete=complete,progress=report,cancelled=cancelled)
        except SearchCancelled:
            raise
        except BuildError as error:
            out.append({"roleId":rid,"error":str(error)});continue
        if res["results"]: reserved.update(res["results"][0]["equipIds"].values())
        res.update(roleId=rid,pick=0,done=False)
        out.append(res)
    return out
