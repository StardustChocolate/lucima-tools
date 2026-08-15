"""多账号会话管理 + 每账号后台调度线程。

每个已登录账号 = 一个 Account 实例，持有自己的 GameClient、开关状态、日志缓冲，
以及一个独立的后台调度线程（每 5 分钟巡检一次已开启的自动任务，各任务内部自带
到点判断；免费刷商店按约 1 小时节流）。账号之间互不干涉。

线程安全：同一账号的 GameClient 不是线程安全的，后台调度线程与前端 API 请求可能
并发访问同一账号 → 每个 Account 持一把 lock，所有对 client 的调用都在锁内进行。
注册表本身的增删也用一把全局锁保护。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import config
from .logutil import log as _log
from .game_client import GameClient
from .tasks import (
    TASKS, run_toggle_tasks, needs_fresh_state, secret_shop_next_free,
    next_claim_times, arena_next_times,
)

# 调度参数。
# **不再是固定 5 分钟盲扫**：绝大多数 toggle 任务都有服务器下发的精确档期
# （reactor=上次领+12h / 生产中心三项=NextCanReceive*Time / dispatch=FinishTime /
#  shop_free=LastResetTime+1h / 竞技场=各 NPC 的 nextMs|retryMs），
# 所以调度线程改成"睡到最早那个到点时刻"，并在用户改开关/改勾选时**立刻唤醒**。
# 只有 abyss_sweep / free_summon 拿不到下次时刻（它们等的是服务器每日重置，时区未确认），
# 那两个本来就是靠 STATE_REFRESH_GAP 每小时重登刷快照来兜——按那个节奏排期即可。
SCAN_MIN_GAP = 60               # 秒：两轮之间的下限（防止到点判定抖动导致空转刷屏）
# 上限=兜底复查。纯保险：档期算漏了/用户在游戏客户端里自己操作导致时刻变了，也不会
# 永远睡下去。复查本身零成本（任务内部自带到点判断，未到点不发请求），所以取和
# STATE_REFRESH_GAP 同量级即可；12 小时的动力转换会被复查约 13 次，无所谓。
SCAN_MAX_GAP = 55 * 60
SHOP_FREE_MIN_GAP = 55 * 60     # 秒：shop_free 最小间隔（约每小时一次）
STATE_REFRESH_GAP = 55 * 60     # 秒：账号快照重新拉取间隔（约每小时一次）
LOG_BUFFER_MAX = 400            # 每账号自动任务日志环形缓冲上限


class Account:
    """一个已登录账号的全部运行时状态。"""

    def __init__(self, email: str, client: GameClient, portal: dict | None):
        self.email = email
        self.client = client
        self.portal = portal
        self.shop_shelf: list | None = None
        self.support_cache: dict = {}         # 活动关卡助战列表原始条目 cuid->entry
        self.lock = threading.Lock()          # 保护对 client 的并发访问

        # 开关 + 参数（从存档恢复；无则空）
        rec = config.load_accounts().get(email, {})
        self.toggles: dict[str, bool] = dict(rec.get("toggles") or {})
        self.params: dict[str, dict] = dict(rec.get("params") or {})

        # 自动任务日志缓冲（后台线程写、前端轮询读）
        self._log: list[dict] = []
        self._log_seq = 0
        self._log_lock = threading.Lock()

        # toggle 任务的跨轮次运行时状态 {task_id: {...}}。目前只有竞技场用：
        # 存每个 NPC 的游戏侧冷却(nextMs)与失败退避(retryMs)。
        # **故意不持久化**——用户要求"刷新和重登都立即重置冷却直接扫荡"，
        # 重登会新建 Account 天然清空；手动刷新走 reset_task_runtime()。
        self.task_runtime: dict[str, dict] = {}

        # 调度线程
        self._shop_free_last = 0.0
        self._state_refresh_last = time.time()   # 刚 bootstrap 过，快照是新的
        self._stop = threading.Event()
        # 配置变更（开关/勾选）时置位，让调度线程立刻醒来重算档期并跑一轮，
        # 用户不用等到下一个到点时刻。见 notify_config_changed()。
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 日志缓冲 ----
    def push_log(self, result: dict, prefix: str):
        """把一条任务结果 + 前缀塞进环形缓冲，带自增 seq 供前端增量拉取。

        ts = 产生时刻（epoch 毫秒）。前端渲染时刻不等于产生时刻：窗口被系统冻结/隐藏
        时轮询会停摆（实测隐藏后节流到 1/分钟、再被彻底冻结），窗口一恢复就把积压的
        记录一次性拉回来，若按渲染时刻打时间戳，几小时前跑的任务会全部显示成"刚刚"，
        看起来像"到点没执行、回来才补跑"。故时刻由产生方给，前端只负责显示。
        """
        with self._log_lock:
            self._log_seq += 1
            self._log.append({"seq": self._log_seq, "prefix": prefix, "result": result,
                              "ts": int(time.time() * 1000)})
            if len(self._log) > LOG_BUFFER_MAX:
                self._log = self._log[-LOG_BUFFER_MAX:]

    def logs_since(self, since: int) -> list[dict]:
        with self._log_lock:
            return [e for e in self._log if e["seq"] > since]

    @property
    def log_seq(self) -> int:
        return self._log_seq

    # ---- 调度线程 ----
    def start_scheduler(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name=f"sched-{self.email}", daemon=True)
        self._thread.start()

    def stop_scheduler(self):
        self._stop.set()
        self._wake.set()        # 把正在等待的线程叫醒，让它看到 _stop 并退出

    def _enabled_ids(self) -> list[str]:
        return [tid for tid, on in self.toggles.items() if on]

    def _due_ids(self) -> list[str]:
        """本轮该跑的任务：所有已开启项，shop_free 额外按游戏侧档期闸门。

        shop_free 没有任务内部的到点判断（每次都真发 ResetRandomStore），所以闸门在这里。
        **以货架 LastResetTime + 1h 为准，不用固定间隔盲等**——原来按 55 分钟节流有个真
        问题：真实冷却是 60 分钟，55 分钟时发出去必被服务器拒（CantReset），而 `_shop_free_last`
        是按"尝试"而非"成功"更新的，于是下一次又要再等 55 分钟 → 实际每 110 分钟才成功一次，
        白丢掉将近一半的免费刷新次数（这也是"漏买强化芯片"的一个来源）。
        改成读档期后：没到点不发请求，到点立刻发，一小时一次不浪费。
        快照里没有货架记录（拿不到档期）时退回固定间隔，宁可慢也不空转。
        """
        next_free = secret_shop_next_free(self.client.account_state or {})
        now_ms = time.time() * 1000
        ids = []
        for tid in self._enabled_ids():
            if tid == "shop_free":
                if next_free is not None:
                    if now_ms < next_free:
                        continue
                elif (time.time() - self._shop_free_last) < SHOP_FREE_MIN_GAP:
                    continue
            ids.append(tid)
        return ids

    def _refresh_state_if_needed(self, ids: list[str]):
        """跑"判定依赖每日重置字段"的任务前，按小时重新拉一次账号快照。

        必要性：`account_state` 只在 bootstrap（登录）时产生，后台巡检一直复用同一份。
        虚拟幻境的意识同步器补充、免费招募的 FreeBuyCount 归零都是**服务器在每日重置
        后于登录时懒更新**的，不重登就永远读到昨天的值——工具挂机过夜后第二天不会
        再执行这两项。这里不去猜"每日重置是几点"（时区未确认，猜错要么空转要么漏跑），
        改成按小时重登刷新，让服务器自己告诉我们最新计数。
        重登失败不影响本轮任务：沿用旧快照，各任务内部的严格判定仍然成立（宁可漏跑）。
        """
        if not needs_fresh_state(ids):
            return
        if (time.time() - self._state_refresh_last) < STATE_REFRESH_GAP:
            return
        self._state_refresh_last = time.time()
        try:
            with self.lock:
                self.client.bootstrap()
        except Exception as e:
            _log.warning("账号 %s 刷新快照失败（沿用旧快照）: %s", self.email, e)

    def run_toggles(self, ids: list[str], prefix: str, log_to_buffer: bool = True) -> list[dict]:
        """在账号锁内执行一批 toggle 任务。log_to_buffer=True 时结果写日志缓冲（后台巡检用）；
        前台手动开关时设 False，由前端直接展示结果、避免与轮询重复。"""
        if not ids:
            return []
        if "shop_free" in ids:
            self._shop_free_last = time.time()
        self._refresh_state_if_needed(ids)
        with self.lock:
            results = run_toggle_tasks(self.client, ids, self.params, self.task_runtime)
        if log_to_buffer:
            for r in results:
                # 只记"确实向服务器发了请求"的结果：到点未满/未到时间而跳过(skipped)的
                # 不产生请求，不入日志，避免巡检刷屏。
                if r.get("skipped"):
                    continue
                self.push_log(r, prefix)
        return results

    def run_due(self, prefix: str = "巡检·") -> list[dict]:
        """跑本轮到点该跑的开关（结果进日志缓冲）。"""
        return self.run_toggles(self._due_ids(), prefix)

    def notify_config_changed(self):
        """开关/参数变了 → 立刻唤醒调度线程（不必等下一个到点时刻）。

        用户拨开竞技场开关后再勾 NPC，勾选本身不触发执行；没有这个唤醒，就要等
        下一轮巡检才动，看起来像"隔了几分钟才有日志"。
        """
        self._wake.set()

    def _next_wake_delay(self) -> float:
        """算出距离"最早一个到点时刻"还有多少秒，夹在 [SCAN_MIN_GAP, SCAN_MAX_GAP]。

        数据源与前端面板同一套 `tasks.next_claim_times`（服务器下发的档期），所以
        调度时刻与界面上显示的倒计时天然一致。
        """
        ids = self._enabled_ids()
        if not ids:
            return SCAN_MAX_GAP          # 没开任何开关：慢速兜底复查即可
        now = time.time()
        now_ms = now * 1000
        try:
            nc = next_claim_times(self.client.account_state or {})
        except Exception:
            nc = {}
        cands: list[float] = []
        for tid in ids:
            if tid == "arena_npc":
                cands.append(self._arena_due_delay(now_ms))
                continue
            if TASKS.get(tid, {}).get("needsFreshState"):
                # 判定依赖每日重置字段，拿不到下次时刻 → 跟着每小时重登的节奏
                cands.append(max(0.0, self._state_refresh_last + STATE_REFRESH_GAP - now))
                continue
            if tid == "shop_free":
                nxt = secret_shop_next_free(self.client.account_state or {})
                if nxt is None:   # 快照里没货架记录 → 退回固定间隔
                    cands.append(max(0.0, self._shop_free_last + SHOP_FREE_MIN_GAP - now))
                else:
                    cands.append(max(0.0, (nxt - now_ms) / 1000))
                continue
            e = nc.get(tid) or {}
            if e.get("blocked"):
                continue                      # 条件不满足（如幻境层数不够）：不排期
            nxt = e.get("nextMs")
            cands.append(0.0 if nxt is None else max(0.0, (nxt - now_ms) / 1000))
        if not cands:
            return SCAN_MAX_GAP
        return max(SCAN_MIN_GAP, min(SCAN_MAX_GAP, min(cands)))

    def _arena_due_delay(self, now_ms: float) -> float:
        """竞技场：勾选的 NPC 里最早一个可挑战时刻（未知/已到点=立刻）。

        闸门与 task_arena_npc 同源：max(快照档期, 扫荡后拿到的 nextMs, 失败退避)。
        """
        rt = self.task_runtime.get("arena_npc") or {}
        picked = (self.params.get("arena_npc") or {}).get("picked") or []
        if not picked:
            return SCAN_MAX_GAP              # 一个都没勾：没事可做
        next_map = rt.get("nextMs") or {}
        retry_map = rt.get("retryMs") or {}
        snap_map = arena_next_times(self.client.account_state or {})
        best = None
        for n in picked:
            sid = f"HellNPC_{int(n)}"
            gate = max(snap_map.get(sid) or 0, next_map.get(sid) or 0,
                       retry_map.get(sid) or 0)
            delay = 0.0 if not gate else max(0.0, (gate - now_ms) / 1000)
            best = delay if best is None else min(best, delay)
            if best == 0.0:
                break
        return SCAN_MAX_GAP if best is None else best

    def reset_task_runtime(self):
        """清空 toggle 任务的运行时状态（手动刷新时调用）。

        用户要求：竞技场"刷新和重登都会立即重置这个冷却直接进行扫荡"。重登天然新建
        Account；手动刷新(/api/status/refresh)走这里。

        ⚠️ 清掉的是**本工具自己记的**失败退避与 nextMs，**不等于无视游戏侧冷却**——
        闸门里还有一份"登录快照算出的档期"(arena_next_times)，而手动刷新本身就是重新
        bootstrap 拉了新快照，所以清完之后仍然只打真正到点的那些。早先没有快照这一份，
        换账号就全部当"可挑战"盲打，每个白扣一面旗帜。
        """
        self.task_runtime.clear()

    def _run_loop(self):
        # 登录后先等一小会，让 bootstrap/首轮登录任务先跑完
        self._stop.wait(5)
        while not self._stop.is_set():
            try:
                self.run_due("巡检·")
            except Exception as e:
                _log.exception("账号 %s 后台巡检异常: %s", self.email, e)
            # 睡到"最早一个到点时刻"，而不是固定 5 分钟盲扫。
            # 期间用户改开关/改勾选会置位 _wake 提前唤醒（notify_config_changed）。
            delay = self._next_wake_delay()
            self._wake.clear()
            woken = self._wake.wait(delay)
            if woken and not self._stop.is_set():
                # 配置刚变过：清掉标志，下一轮循环立即执行
                self._wake.clear()

    def close(self):
        self.stop_scheduler()
        try:
            with self.lock:
                self.client.close()
        except Exception:
            pass


# ---------- 注册表 ----------
_accounts: dict[str, Account] = {}
_reg_lock = threading.Lock()


def get(email: str) -> Account | None:
    with _reg_lock:
        return _accounts.get(email)


def all_accounts() -> list[Account]:
    with _reg_lock:
        return list(_accounts.values())


def add(email: str, client: GameClient, portal: dict | None) -> Account:
    """登录成功后登记账号；若同 email 已在线，先关掉旧的再替换。"""
    with _reg_lock:
        old = _accounts.get(email)
        if old:
            old.close()
        acc = Account(email, client, portal)
        _accounts[email] = acc
    acc.start_scheduler()
    return acc


def remove(email: str) -> bool:
    """下线并从注册表移除（不删存档，存档另由 config.delete_account 处理）。"""
    with _reg_lock:
        acc = _accounts.pop(email, None)
    if acc:
        acc.close()
        return True
    return False
