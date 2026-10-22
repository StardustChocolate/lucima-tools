"""多账号会话管理 + 每账号后台调度线程。

每个已登录账号 = 一个 Account 实例，持有自己的 GameClient、开关状态、日志缓冲，
以及一个独立的后台调度线程（按已开启任务的最近档期唤醒，各任务内部自带
到点判断，并定期读取缓存复查）。账号之间互不干涉。

线程安全：同一账号的 GameClient 不是线程安全的，后台调度线程与前端 API 请求可能
并发访问同一账号 → 每个 Account 持一把 lock，所有对 client 的调用都在锁内进行。
注册表本身的增删也用一把全局锁保护。
"""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any, Callable

from . import config
from .logutil import log as _log
from . import redeem_codes
from .game_client import GameClient, GameError, GameMessage
from .guild import (CAPTAIN_BUFF_STATUS_SYNC_MS, CaptainBuffStatusPending,
                    CaptainShopInsufficientCurrency, GuildService, has_guild,
                    describe_error, donation_options,
                    donation_reserves, GUILD_DONATIONS, is_auth_error)
from .guild_aid import GuildAidService, aid_catalog, aid_options
from .guild_quests import GuildWeeklyService
from .guild_store import GuildStoreService, store_options
from .guild_captain_store import GuildCaptainStoreService, captain_store_options
from .tasks import (
    TASKS, run_auto_tasks, run_toggle_tasks, next_claim_times, arena_next_times,
    achievement_options, TASK_RETRY_MS, SHOP_FREE_COOLDOWN_MS,
)

# 调度参数。
# **不再是固定 5 分钟盲扫**：绝大多数 toggle 任务都有服务器下发的精确档期
# （reactor=上次领+12h / 生产中心三项=NextCanReceive*Time / dispatch=FinishTime /
#  shop_free=LastResetTime+1h1s / 竞技场=各 NPC 的 nextMs|retryMs），
# 所以调度线程改成"睡到最早那个到点时刻"，并在用户改开关/改勾选时**立刻唤醒**。
# 只有 abyss_sweep / free_summon 拿不到服务器下发的下次时刻。它们依赖每日重置后的
# 登录快照，因此在东八区每天 08:10~08:30 为每个账号随机安排一次重登。
SCAN_MIN_GAP = 60               # 秒：已到期或缺少档期时防止空转；未来档期按时唤醒
# 上限=兜底复查，避免档期遗漏后一直休眠。只读取本地缓存检查任务条件，
# 复查本身不刷新服务器快照，未到期的任务不会发请求。
SCAN_MAX_GAP = 60 * 60         # 秒：没有更早档期时，每 60 分钟复查缓存
SHOP_FREE_MIN_GAP = SHOP_FREE_COOLDOWN_MS // 1000  # 秒：缺时间戳时同样等待 1 小时 1 秒
LOG_BUFFER_MAX = 400            # 每账号自动任务日志环形缓冲上限
GAME_TIMEZONE = timezone(timedelta(hours=8))
STATE_REFRESH_BOUNDARY = datetime_time(8, 0)
STATE_REFRESH_WINDOW_START = datetime_time(8, 10)
STATE_REFRESH_WINDOW_SECONDS = 20 * 60


def _scheduler_time(value: float) -> str:
    return datetime.fromtimestamp(value, GAME_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _scheduler_runtime_status() -> str:
    """Return native Android keep-alive state without making it a backend dependency."""
    if config.PLATFORM != "android":
        return f"platform={config.PLATFORM}"
    try:
        from java import jclass  # type: ignore[import-not-found]
        service = jclass("com.openrubi.lucimatools.AutomationService")
        return str(service.schedulerDiagnostics())
    except Exception as error:
        return f"platform=android nativeStatus=unavailable({type(error).__name__})"


def _next_daily_state_refresh(now: datetime | None = None) -> float:
    """返回下一次每日快照重登时间戳，固定使用游戏所在的东八区。"""
    current = (now or datetime.now(GAME_TIMEZONE)).astimezone(GAME_TIMEZONE)
    target_date = current.date()
    reset_boundary = datetime.combine(target_date, STATE_REFRESH_BOUNDARY, GAME_TIMEZONE)
    # 08:00 后建立的会话已经拿到当日重置数据，不再在当天重复登录。
    if current >= reset_boundary:
        target_date += timedelta(days=1)
    window_start = datetime.combine(target_date, STATE_REFRESH_WINDOW_START, GAME_TIMEZONE)
    return (window_start + timedelta(seconds=random.randint(0, STATE_REFRESH_WINDOW_SECONDS))).timestamp()


def _next_action_ms(entry: dict):
    """A skill module can become actionable before its production finishes."""
    deadline = entry.get("nextMs")
    if not entry.get("retrying"):
        charge_ms = (entry.get("charge") or {}).get("nextMs")
        if charge_ms is not None:
            deadline = charge_ms if deadline is None else min(deadline, charge_ms)
    return deadline


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

        # toggle 任务的跨轮次运行时状态：失败退避、竞技场冷却、体力邮件确认等。
        # 不持久化；新会话由服务器快照还原档期。
        self.task_runtime: dict[str, dict] = {}

        # 调度线程
        self._shop_free_last = 0.0
        self._state_refresh_at = _next_daily_state_refresh()
        self.state_revision = 1
        self.state_synced_at = int(time.time() * 1000)
        self.guild_runtime: dict = {"stateSyncedAtMs": self.state_synced_at}
        self.guild_runtime["weeklyPersonalWeek"] = ((self.client.account_state or {}).get("PlayerGuildInfo") or {}).get("NowGuildQuestWeek")
        self._guild_revision = 0
        self._meal_check_after_ms = self.state_synced_at
        self._send_meal_enabled = bool(self.toggles.get("send_meal"))
        self.state_sync_error: str | None = None
        self._quest_update_revision = 0
        self._quest_checked_revision = 0
        self._daily_achievement_enabled = bool(self.toggles.get('daily_achievement'))
        self._achievement_options = achievement_options(self.params.get("daily_achievement"))
        self._quest_check_pending = self._daily_achievement_enabled and any(self._achievement_options.values())
        self._redeem_checked_revision = 0
        self._redeem_pending = bool(self.toggles.get("redeem_code"))
        self.client.quest_update_callback = self._on_quest_updates
        self._stop = threading.Event()
        # 配置变更（开关/勾选）时置位，让调度线程立刻醒来重算档期并跑一轮，
        # 用户不用等到下一个到点时刻。见 notify_config_changed()。
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._scheduler_lock = threading.Lock()
        self._startup_done = False
        self._auto_relogin_transition_ms: int | None = None
        self._auto_relogin_at: float | None = None
        self._auto_relogin_delay_seconds: int | None = None
        self.client.connection_changed_callback = self._wake.set
        self.client.inventory_increase_callback = self._on_inventory_increase
        self.client.guild_currency_increase_callback = self._on_guild_currency_increase

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
    def start_scheduler(self) -> bool:
        """主界面就绪后启动任务；重复请求不会重复执行首轮任务。"""
        with self._scheduler_lock:
            if self._stop.is_set():
                raise RuntimeError("账号已下线")
            if self._thread and self._thread.is_alive():
                return False
            self._thread = threading.Thread(
                target=self._run_loop, name=f"sched-{self.email}", daemon=True)
            self._thread.start()
            return True

    def stop_scheduler(self):
        with self._scheduler_lock:
            self._stop.set()
            self._wake.set()    # 把正在等待的线程叫醒，让它看到 _stop 并退出

    def _run_startup_tasks(self):
        """首轮自动任务在后台执行，每项结果直接写入日志缓冲。"""
        from .updater import service as update_service

        def should_stop():
            return self._stop.is_set() or update_service.is_installing()

        if should_stop():
            return
        with self.lock:
            run_auto_tasks(self.client,
                           on_result=lambda result: self.push_log(result, "自动·"),
                           should_stop=should_stop)
        if not should_stop():
            redeem_codes.refresh_if_due(initial=True)

    def _enabled_ids(self) -> list[str]:
        # Guild rewards belong to the guild page and its entry flow.
        return [tid for tid, on in self.toggles.items()
                if on and tid not in {"guild_auto_signin", "guild_auto_donate", "guild_auto_aid", "guild_auto_weekly", "guild_auto_store", "guild_auto_captain_store"}]

    def guild_snapshot(self) -> dict:
        service = GuildService(self.client, self.guild_runtime,
                               donation_reserves(self.params.get("guild_auto_donate")))
        return {**service.snapshot(), "aid": GuildAidService(service).snapshot(),
                "weekly": GuildWeeklyService(service).snapshot(),
                "store": GuildStoreService(service).snapshot(),
                "captainStore": GuildCaptainStoreService(service).snapshot(),
                "loginRevision": self.state_revision, "revision": self._guild_revision}

    def _guild_due_delay(self) -> float | None:
        if not has_guild(self.client.account_state):
            return None
        runtime = self.guild_runtime
        pending = runtime.get("enteredRevision") != self.state_revision
        if not pending and self.toggles.get("guild_auto_signin") and not runtime.get("claimBlocked"):
            state = self.guild_snapshot()
            pending = state["checkedIn"] and state["canClaim"]
        if not pending and self.toggles.get("guild_auto_donate"):
            options = donation_options(self.params.get("guild_auto_donate"))
            errors = runtime.get("donationErrors") or {}
            pending = any(options[key] and row["count"] > 0 and key not in errors
                          for key, row in GuildService(
                              self.client, runtime,
                              donation_reserves(self.params.get("guild_auto_donate"))
                          ).donation_status().items())
        delays = [max(0.0, (runtime.get("retryAtMs", 0) / 1000) - time.time())] if pending else []
        if not pending and self.toggles.get("guild_auto_aid"):
            aid = GuildAidService(GuildService(self.client, runtime))
            delay = aid.next_delay(
                aid_options(self.params.get("guild_auto_aid")))
            if delay is not None:
                if aid.needs_query():
                    delay = max(delay, (runtime.get("guildTaskQueryRetryAtMs", 0) / 1000) - time.time())
                delays.append(delay)
        if not pending and self.toggles.get("guild_auto_weekly"):
            weekly = GuildWeeklyService(GuildService(self.client, runtime))
            delay = weekly.next_delay()
            if delay is not None:
                if weekly.needs_query():
                    delay = max(delay, (runtime.get("guildTaskQueryRetryAtMs", 0) / 1000) - time.time())
                delays.append(delay)
        if not pending and self.toggles.get('guild_auto_store'):
            delay = GuildStoreService(GuildService(self.client, runtime)).next_delay(
                self.params.get('guild_auto_store'))
            if delay is not None:
                delays.append(delay)
        if not pending and self.toggles.get('guild_auto_captain_store'):
            delay = GuildCaptainStoreService(GuildService(self.client, runtime)).next_delay(
                self.params.get('guild_auto_captain_store'))
            if delay is not None:
                delays.append(delay)
        return min(delays) if delays else None

    def refresh_guild_locked(self, *, log_success=True):
        """Call under the account lock; manual refresh and login share all gates."""
        runtime = self.guild_runtime
        service = GuildService(self.client, runtime,
                               donation_reserves(self.params.get("guild_auto_donate")))
        runtime.update(loading=True, error=None)
        self._guild_revision += 1
        try:
            if not has_guild(self.client.account_state):
                runtime["enteredRevision"] = self.state_revision
                return
            checked = service.enter().get("checkedIn", False)
            runtime["enteredRevision"] = self.state_revision
            runtime.pop("claimBlocked", None)
            runtime.pop("claimError", None)
            runtime.pop("donationErrors", None)
            if checked:
                self.push_log({"id": "guild_checkin", "name": "佣兵团报到", "ok": True,
                               "detail": "今日首次进入佣兵团，自动报到完成"}, "自动·")
            if log_success:
                self.push_log({"id": "guild_info", "name": "佣兵团", "ok": True,
                               "detail": "佣兵团信息已更新"}, "自动·")
            runtime.pop("retryAtMs", None)
            GuildAidService(service).note_synced()
            GuildWeeklyService(service).note_synced()
            runtime.pop("guildTaskQueryRetryAtMs", None)
        except Exception as error:
            # Re-query before retrying an unconfirmed mutation. The live own
            # member record tells us whether the previous request committed.
            runtime.pop("enteredRevision", None)
            runtime["error"] = describe_error(error)
            runtime["retryAtMs"] = int(time.time() * 1000) + TASK_RETRY_MS
            raise
        finally:
            runtime["loading"] = False
            self._guild_revision += 1
        self._claim_guild_locked()
        self._donate_guild_locked()
        self._aid_guild_locked()
        self._weekly_guild_locked()
        self._store_guild_locked()

    def _claim_guild_locked(self):
        """Claim from cached eligibility only; never turn a toggle into a query."""
        runtime = self.guild_runtime
        if not self.toggles.get("guild_auto_signin") or runtime.get("claimBlocked"):
            return
        service = GuildService(self.client, runtime,
                               donation_reserves(self.params.get("guild_auto_donate")))
        state = service.snapshot()
        if not state["joined"] or not state["checkedIn"] or not state["canClaim"]:
            return
        runtime.update(claiming=True, claimError=None)
        self._guild_revision += 1
        try:
            if service.claim_daily_if_due():
                self.push_log({"id": "guild_auto_signin", "name": "佣兵团自动签到", "ok": True,
                               "detail": "已领取佣兵团日奖励"}, "自动·")
        except Exception as error:
            # A timeout can mean the reward was already committed. Keep the
            # cached state, and wait for the next normal sync before reusing it.
            runtime.update(claimBlocked=True, claimError=describe_error(error))
            self.push_log({"id": "guild_auto_signin", "name": "佣兵团自动签到", "ok": False,
                           "detail": f"{describe_error(error)}，等待下次登录或手动刷新后确认"}, "自动·")
        finally:
            runtime["claiming"] = False
            self._guild_revision += 1

    def _run_guild_if_due(self):
        from .updater import service as update_service
        delay = self._guild_due_delay()
        if (delay is None or delay > 0 or self._stop.is_set()
                or update_service.is_installing() or not self.client.is_connected()):
            return
        with self.lock:
            delay = self._guild_due_delay()
            if (delay is None or delay > 0 or self._stop.is_set()
                    or update_service.is_installing() or not self.client.is_connected()):
                return
            try:
                if self.guild_runtime.get("enteredRevision") != self.state_revision:
                    self.refresh_guild_locked(log_success=False)
                else:
                    self._claim_guild_locked()
                    self._donate_guild_locked()
                    self._aid_guild_locked()
                    self._weekly_guild_locked()
                    self._store_guild_locked()
                    self._store_captain_guild_locked()
            except Exception as error:
                self.push_log({"id": "guild_info", "name": "佣兵团", "ok": False,
                               "detail": f"{describe_error(error)}，5 分钟后重新查询"}, "自动·")

    def _donate_guild_locked(self):
        """Only spend selected resources within confirmed daily and inventory limits."""
        from .updater import service as update_service
        runtime = self.guild_runtime
        service = GuildService(self.client, runtime,
                               donation_reserves(self.params.get("guild_auto_donate")))
        for key, rule in GUILD_DONATIONS.items():
            # Re-read settings between requests so disabling takes effect immediately.
            if (self._stop.is_set() or update_service.is_installing() or not self.client.is_connected()
                    or not self.toggles.get("guild_auto_donate")):
                return
            if (not donation_options(self.params.get("guild_auto_donate"))[key]
                    or key in (runtime.get("donationErrors") or {})
                    or not service.donation_status()[key]["count"]):
                continue
            runtime["donating"] = key
            self._guild_revision += 1
            try:
                count = service.donate_if_due(key)
                if count:
                    self.push_log({"id": "guild_auto_donate", "name": "佣兵团自动捐赠", "ok": True,
                                   "detail": f"已捐赠{rule['name']} ×{count * rule['cost']:,}（{count} 次）"}, "自动·")
            except Exception as error:
                # Do not repeat an uncertain spend from stale counters. A normal
                # login / manual refresh will confirm it and unblock this resource.
                runtime["donationErrors"] = {**(runtime.get("donationErrors") or {}), key: describe_error(error)}
                self.push_log({"id": "guild_auto_donate", "name": "佣兵团自动捐赠", "ok": False,
                               "detail": f"{rule['name']}：{describe_error(error)}，等待下次登录或手动刷新后确认"}, "自动·")
                if is_auth_error(error):
                    return
            finally:
                runtime.pop("donating", None)
                self._guild_revision += 1

    def _aid_guild_locked(self):
        """Use entry data immediately, then fetch new aid records once an hour."""
        from .updater import service as update_service
        runtime = self.guild_runtime
        service = GuildService(self.client, runtime)
        aid = GuildAidService(service)

        def options():
            return aid_options(self.params.get("guild_auto_aid"))

        def stopped():
            return (not self.toggles.get("guild_auto_aid") or self._stop.is_set()
                    or update_service.is_installing() or not self.client.is_connected()
                    or not has_guild(self.client.account_state))

        def pause():
            delay = config.get_request_delay_config()
            jitter = delay["jitter"] / 100
            remaining = delay["ms"] / 1000 * random.uniform(1 - jitter, 1 + jitter) if delay["enabled"] else 0
            while remaining > 0 and not stopped():
                step = min(.2, remaining)
                self._stop.wait(step)
                remaining -= step
            return not stopped()

        if stopped():
            return
        delay = aid.next_delay(options())
        if delay is None or delay > 0:
            return
        count = reward = 0
        sent = False
        self._guild_revision += 1
        try:
            if aid.needs_query():
                runtime["aiding"] = "query"
                if time.time() * 1000 < runtime.get("guildTaskQueryRetryAtMs", 0):
                    return
                self._sync_guild_tasks_locked(service)
                if not runtime.get("aidSynced"):
                    raise GameError("佣兵团援助查询缺少本人状态或求助列表")
            if stopped():
                return
            current = options()
            if aid.request_due(current):
                runtime["aiding"] = "request"
                if aid.request(current):
                    sent = True
                    name = aid_catalog()["items"][current["item"]]["name"]
                    self.push_log({"id": "guild_auto_aid", "name": "佣兵团自动援助", "ok": True,
                                   "detail": f"已发布求助：{name}"}, "自动·")
            # Recompute from the latest response, inventory, reward and settings
            # after every operation. Each request addresses one guild aid record.
            while not stopped():
                current = options()
                candidates = aid.candidates(current)
                if not candidates:
                    break
                if sent:
                    if not pause():
                        break
                    current = options()
                    candidates = aid.candidates(current)
                    if not candidates:
                        break
                runtime["aiding"] = "support"
                gained = aid.support(candidates[0], current)
                if not gained:
                    break
                count += 1
                reward += gained
                sent = True
        except Exception as error:
            runtime["aidError"] = describe_error(error)
            runtime["aidRetryAtMs"] = int(time.time() * 1000) + TASK_RETRY_MS
            runtime["aidSynced"] = False
            self.push_log({"id": "guild_auto_aid", "name": "佣兵团自动援助", "ok": False,
                           "detail": f"{describe_error(error)}，5 分钟后查询确认"}, "自动·")
        finally:
            if count:
                self.push_log({"id": "guild_auto_aid", "name": "佣兵团自动援助", "ok": True,
                               "detail": f"自动援助完成：{count} 次，获得勇气纹章 ×{reward}"}, "自动·")
            runtime.pop("aiding", None)
            self._guild_revision += 1

    def _sync_guild_tasks_locked(self, service):
        """One hourly query refreshes both aid and weekly rewards."""
        try:
            response = service.query()
            if not isinstance(response.get("GuildData"), dict):
                raise GameError("佣兵团查询缺少团内资料")
            service.checkin_if_due()
            GuildAidService(service).note_synced()
            GuildWeeklyService(service).note_synced()
            self.guild_runtime.pop("guildTaskQueryRetryAtMs", None)
        except Exception:
            self.guild_runtime["guildTaskQueryRetryAtMs"] = int(time.time() * 1000) + TASK_RETRY_MS
            raise

    def _weekly_guild_locked(self):
        if self.guild_runtime.get("weeklyScoreJob"):
            return
        from .updater import service as update_service
        runtime = self.guild_runtime
        service = GuildService(self.client, runtime)
        weekly = GuildWeeklyService(service)

        def stopped():
            return (not self.toggles.get("guild_auto_weekly") or self._stop.is_set()
                    or update_service.is_installing() or not self.client.is_connected()
                    or not has_guild(self.client.account_state))

        def pause():
            delay = config.get_request_delay_config()
            jitter = delay["jitter"] / 100
            remaining = delay["ms"] / 1000 * random.uniform(1 - jitter, 1 + jitter) if delay["enabled"] else 0
            while remaining > 0 and not stopped():
                step = min(.2, remaining)
                self._stop.wait(step)
                remaining -= step
            return not stopped()

        delay = weekly.next_delay() if not stopped() else None
        if delay is None or delay > 0:
            return
        self._guild_revision += 1
        try:
            sent = False
            if weekly.needs_query():
                if time.time() * 1000 < runtime.get("guildTaskQueryRetryAtMs", 0):
                    return
                runtime["weeklyRunning"] = "query"
                self._sync_guild_tasks_locked(service)
                sent = True
                if not runtime.get("weeklySynced"):
                    raise GameError("周任务查询缺少本周任务或本人领奖记录")
            # The server-selected IDs may change with a reset. Recheck after
            # every response instead of retaining last week's candidates.
            while not stopped():
                ready = [row for row in weekly.snapshot()["personal"] if row["state"] == "ready"]
                if not ready:
                    break
                if sent and not pause():
                    return
                runtime["weeklyRunning"] = "personal"
                if weekly.claim_personal(ready[0]["id"]):
                    self.push_log({"id": "guild_auto_weekly", "name": "佣兵团周任务", "ok": True,
                                   "detail": f"已领取周任务：{ready[0]['name']}"}, "自动·")
                sent = True
            if not stopped() and any(row["state"] == "ready" for row in weekly.snapshot()["tiers"]):
                if sent and not pause():
                    return
                runtime["weeklyRunning"] = "tiers"
                count = weekly.claim_tiers()
                if count:
                    self.push_log({"id": "guild_auto_weekly", "name": "佣兵团周任务", "ok": True,
                                   "detail": f"已领取全团积分奖励：{count} 档"}, "自动·")
        except Exception as error:
            runtime.update(weeklyError=describe_error(error), weeklySynced=False,
                           weeklyRetryAtMs=int(time.time() * 1000) + TASK_RETRY_MS)
            self.push_log({"id": "guild_auto_weekly", "name": "佣兵团周任务", "ok": False,
                           "detail": f"{describe_error(error)}，5 分钟后查询确认"}, "自动·")
        finally:
            runtime.pop("weeklyRunning", None)
            self._guild_revision += 1

    def _on_inventory_increase(self, identifiers):
        if self.toggles.get('guild_auto_store') and set(identifiers) & self.guild_runtime.get('shopWatchCurrencies', set()):
            self._wake.set()

    def _on_guild_currency_increase(self, identifiers):
        watched = self.guild_runtime.get('captainShopWatchCurrencies', set())
        if (self.toggles.get('guild_auto_captain_store')
                and set(identifiers) & watched):
            self._wake.set()

    def _store_guild_locked(self):
        from .updater import service as update_service
        runtime = self.guild_runtime
        store = GuildStoreService(GuildService(self.client, runtime))

        def stopped():
            return (self._stop.is_set() or update_service.is_installing()
                    or not self.client.is_connected() or not self.toggles.get('guild_auto_store'))

        def pause():
            delay = config.get_request_delay_config()
            remaining = max(0, (delay['ms'] + random.randint(-delay['jitter'], delay['jitter'])) / 1000) if delay['enabled'] else 0
            while remaining > 0 and not stopped():
                step = min(0.1, remaining)
                self._stop.wait(step)
                remaining -= step

        if stopped():
            runtime['shopWatchCurrencies'] = set()
            return
        sent = False
        while not stopped():
            if sent:
                pause()
                if stopped():
                    break
            selected = set(store_options(self.params.get('guild_auto_store')))
            row = next((r for r in store.snapshot()['goods'] if r['id'] in selected and r['buyable']), None)
            if row is None:
                break
            runtime['shopRunning'] = row['id']
            self._guild_revision += 1
            try:
                count = store.buy(row['id'], row['buyable'], automatic=True)
                self.push_log({'id': 'guild_auto_store', 'name': '团员商店', 'ok': True,
                               'detail': f"已购买{row['name']} ×{count * row['amount']}"}, '自动·')
            except Exception as error:
                runtime.setdefault('shopUncertain', {}).setdefault(row['id'], True)
                self.push_log({'id': 'guild_auto_store', 'name': '团员商店', 'ok': False,
                               'detail': f"{row['name']}：{describe_error(error)}，等待重新登录确认"}, '自动·')
                if is_auth_error(error):
                    break
            finally:
                runtime.pop('shopRunning', None)
                self._guild_revision += 1
            sent = True
        if self.toggles.get('guild_auto_store'):
            store.next_delay(self.params.get('guild_auto_store'))
        else:
            runtime['shopWatchCurrencies'] = set()

    def _store_captain_guild_locked(self):
        from .updater import service as update_service
        runtime = self.guild_runtime
        store = GuildCaptainStoreService(GuildService(self.client, runtime))

        def stopped():
            return (self._stop.is_set() or update_service.is_installing()
                    or not self.client.is_connected() or not self.toggles.get('guild_auto_captain_store'))

        def pause():
            delay = config.get_request_delay_config()
            remaining = max(0, (delay['ms'] + random.randint(-delay['jitter'], delay['jitter'])) / 1000) if delay['enabled'] else 0
            while remaining > 0 and not stopped():
                step = min(0.1, remaining)
                self._stop.wait(step)
                remaining -= step

        if stopped():
            runtime['captainShopWatchCurrencies'] = set()
            return

        now_ms = int(time.time() * 1000)
        pending_status = runtime.get('captainShopBuffStatusPending') or {}
        due_status = [identifier for identifier in pending_status
                      if now_ms >= (runtime.get('captainShopStatusSyncRetryAtMs') or {}).get(identifier, 0)]
        for identifier in due_status:
            try:
                store.guild._call('ServerStatusHandler.QueryGuildStatus')
            except Exception:
                runtime.setdefault('captainShopStatusSyncRetryAtMs', {})[identifier] = (
                    int(time.time() * 1000) + CAPTAIN_BUFF_STATUS_SYNC_MS)
                continue
            if identifier in (runtime.get('captainShopBuffStatusPending') or {}):
                runtime.setdefault('captainShopStatusSyncRetryAtMs', {})[identifier] = (
                    int(time.time() * 1000) + CAPTAIN_BUFF_STATUS_SYNC_MS)

        sent = False
        while not stopped():
            if sent:
                pause()
                if stopped():
                    break
            selected = set(captain_store_options(self.params.get('guild_auto_captain_store')))
            retry = runtime.setdefault('captainShopRetryAtMs', {})
            now = int(time.time() * 1000)
            row = next((item for item in store.snapshot()['goods']
                        if item['id'] in selected and item['available'] and now >= retry.get(item['id'], 0)), None)
            if row is None:
                break
            runtime['captainShopRunning'] = row['id']
            self._guild_revision += 1
            try:
                store.buy(row['id'])
                retry.pop(row['id'], None)
                self.push_log({'id': 'guild_auto_captain_store', 'name': '团长商店', 'ok': True,
                               'detail': f"已购买{row['name']}"}, '自动·')
            except CaptainShopInsufficientCurrency as error:
                retry.pop(row['id'], None)
                runtime.setdefault('captainShopWatchCurrencies', set()).add(error.currency)
                self.push_log({'id': 'guild_auto_captain_store', 'name': '团长商店', 'ok': False,
                               'detail': f'{row["name"]}：{describe_error(error)}，等待公共货币余额充足'}, '自动·')
            except CaptainBuffStatusPending as error:
                runtime.setdefault('captainShopBuffStatusPending', {})[row['id']] = (
                    runtime.get('captainShopBuffStatusPending', {}).get(row['id']) or
                    {'afterMs': int(time.time() * 1000)})
                runtime.setdefault('captainShopStatusSyncRetryAtMs', {})[row['id']] = (
                    int(time.time() * 1000) + CAPTAIN_BUFF_STATUS_SYNC_MS)
                self.push_log({'id': 'guild_auto_captain_store', 'name': '团长商店', 'ok': False,
                               'detail': f'{row["name"]}：{describe_error(error)}'}, '自动·')
            except Exception as error:
                if isinstance(error, GameMessage) or (isinstance(error, GameError)
                                                       and '未发送购买请求' in str(error)):
                    retry[row['id']] = now + TASK_RETRY_MS
                    detail = f"{row['name']}：{describe_error(error)}，5 分钟后重试"
                else:
                    runtime.setdefault('captainShopUncertain', {})[row['id']] = True
                    detail = f"{row['name']}：{describe_error(error)}，购买结果未确认；等待下次登录确认"
                self.push_log({'id': 'guild_auto_captain_store', 'name': '团长商店', 'ok': False,
                               'detail': detail}, '自动·')
                if is_auth_error(error):
                    break
            finally:
                runtime.pop('captainShopRunning', None)
                self._guild_revision += 1
            sent = True
        if self.toggles.get('guild_auto_captain_store'):
            store.next_delay(self.params.get('guild_auto_captain_store'))
        else:
            runtime['captainShopWatchCurrencies'] = set()

    def _note_guild_login(self):
        # Drop the old server clock after a new authoritative login snapshot.
        self.guild_runtime.clear()
        self.guild_runtime["stateSyncedAtMs"] = self.state_synced_at
        self.guild_runtime["weeklyPersonalWeek"] = ((self.client.account_state or {}).get("PlayerGuildInfo") or {}).get("NowGuildQuestWeek")
        self._guild_revision += 1

    def _claim_runtime(self) -> dict:
        """只读合并账号日程，不把查看倒计时变成一次任务状态写入。"""
        meal = {**(self.task_runtime.get("send_meal") or {}),
                "checkAfterMs": max(self.state_synced_at, self._meal_check_after_ms),
                "stateRefreshAtMs": int(self._state_refresh_at * 1000)}
        signin = {**(self.task_runtime.get("month_signin") or {}),
                  "loginRevision": self.state_revision,
                  "stateSyncedAtMs": self.state_synced_at,
                  "stateRefreshAtMs": int(self._state_refresh_at * 1000)}
        return {**self.task_runtime, "send_meal": meal, "month_signin": signin}

    def _queue_meal_check(self):
        """登录数据刷新或重新开启后立即检查，保留已经投递、等待确认的餐次。"""
        self._meal_check_after_ms = int(time.time() * 1000)
        runtime = self.task_runtime.get("send_meal") or {}
        runtime.pop("nextRetryMs", None)
        runtime.pop("retryWindowMs", None)

    def next_claims(self, now_ms: int | None = None) -> dict:
        """界面、调度与后台限制计时共用的账号任务档期。"""
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        claims = next_claim_times(self.client.account_state or {}, self.params,
                                 runtime=self._claim_runtime(), now_ms=now_ms)
        shop = claims.get("shop_free") or {}
        if (shop.get("nextMs") is None and self._shop_free_last
                and not self._retry_at_ms("shop_free")):
            shop["nextMs"] = int((self._shop_free_last + SHOP_FREE_MIN_GAP) * 1000)
            shop["ready"] = now_ms >= shop["nextMs"]
        for task_id, entry in claims.items():
            retry_at = self._retry_at_ms(task_id)
            if not retry_at or entry.get("blocked"):
                continue
            # 免费额度已用尽时不能把一次失败变成下一次付费招募。
            if TASKS.get(task_id, {}).get("needsFreshState") and not entry.get("ready"):
                continue
            deadline = _next_action_ms(entry)
            entry["nextMs"] = max(deadline or now_ms, retry_at)
            entry["ready"] = now_ms >= entry["nextMs"]
            entry["retrying"] = True
            charge = entry.get("charge")
            if charge and charge.get("nextMs") is not None:
                charge.update(ready=False, retrying=True)
        return claims

    def _retry_at_ms(self, task_id: str) -> int:
        return int((self.task_runtime.get(task_id) or {}).get("retryAtMs") or 0)

    def _on_quest_updates(self):
        self._quest_update_revision += 1
        self._guild_revision += 1
        if self.toggles.get("guild_auto_weekly"):
            self._wake.set()
        if (self.toggles.get('daily_achievement')
                and any(achievement_options(self.params.get("daily_achievement")).values())):
            self._quest_check_pending = True
            self._wake.set()

    def _due_ids(self) -> list[str]:
        """本轮该跑的已开启任务，统一遵守失败退避。

        shop_free 没有任务内部的到点判断（每次都真发 ResetRandomStore），所以闸门在这里。
        以货架 LastResetTime + 1h1s 为准；缺少时间戳时也按 1 小时 1 秒兜底。
        请求失败则按 5 分钟重试，并继续遵守已有的游戏冷却。
        """
        now_ms = int(time.time() * 1000)
        claims = self.next_claims(now_ms)
        ids = []
        for tid in self._enabled_ids():
            if now_ms < self._retry_at_ms(tid):
                continue
            if tid in {"send_meal", "month_signin"}:
                if (claims.get(tid) or {}).get("ready"):
                    ids.append(tid)
                continue
            if tid == "redeem_code":
                retry_at = (self.task_runtime.get("redeem_code") or {}).get("nextRetryAt") or 0
                if self._redeem_pending and time.time() >= retry_at:
                    ids.append(tid)
                continue
            if tid == 'daily_achievement':
                if not any(achievement_options(self.params.get("daily_achievement")).values()):
                    continue
                if not self._quest_check_pending and self._quest_update_revision <= self._quest_checked_revision:
                    continue
                ids.append(tid)
                continue
            if tid == "shop_free":
                if not (claims.get(tid) or {}).get("ready"):
                    continue
            if tid == "sf_tesseract":
                entry = claims.get(tid) or {}
                if not (entry.get("ready") or (entry.get("charge") or {}).get("ready")):
                    continue
            ids.append(tid)
        return ids

    def _refresh_state_if_due(self) -> bool:
        """每天独立重新拉一次账号快照，不依赖任何任务开关。

        必要性：`account_state` 只在 bootstrap（登录）时产生，后台巡检一直复用同一份。
        除虚拟幻境、免费招募外，静态商店的 BuyCount 也只能由新登录快照权威刷新。
        游戏在 08:00 前完成重置，因此每个在线账号在东八区每天 08:10~08:30 随机
        成功重登一次。失败时保留旧快照，5 分钟后重试，不在本地擅自清零任何计数。
        """
        from .updater import service as update_service
        if self._stop.is_set() or update_service.is_installing() or time.time() < self._state_refresh_at:
            return False
        with self.lock:
            # 手动刷新可能刚在等待锁期间完成；拿到锁后必须重新检查计划。
            now = time.time()
            if self._stop.is_set() or now < self._state_refresh_at:
                return False
            if self._auto_relogin_delay(now) is not None:
                return False
            try:
                self.client.bootstrap()
            except Exception as e:
                # 首次失败仍允许旧会话检查日间体力；连续补登失败不清除体力的退避。
                if self.state_sync_error is None:
                    self._queue_meal_check()
                self.state_sync_error = str(e)
                self._state_refresh_at = time.time() + TASK_RETRY_MS / 1000
                _log.warning("账号 %s 刷新快照失败（沿用旧快照）: %s；下次重试=%s",
                             self.email, e, _scheduler_time(self._state_refresh_at))
                self.push_log({"id": "state_refresh", "name": "每日重登", "ok": False,
                               "detail": f"重登失败：{e}，沿用旧快照，"
                                         f"{TASK_RETRY_MS // 60_000} 分钟后重试"}, "自动·")
                return False
            # 成功取得当日快照后才排到下一个重置日；手动重登也会重排同一档期。
            self._sync_arena_after_login()
            self._state_refresh_at = _next_daily_state_refresh()
            self.state_revision += 1
            self.state_synced_at = int(time.time() * 1000)
            self._note_guild_login()
            self._queue_meal_check()
            # 新登录立即处理每日签到；上一会话的失败退避不延迟这次触发。
            (self.task_runtime.get("month_signin") or {}).pop("retryAtMs", None)
            self.state_sync_error = None
            self._on_quest_updates()
            _log.info("账号 %s 每日快照刷新完成 revision=%s", self.email,
                      self.state_revision)
            return True

    def _auto_relogin_delay(self, now: float | None = None) -> float | None:
        """Return time until recovery; None means no disconnected-session recovery."""
        now = time.time() if now is None else now
        cfg = config.get_auto_relogin_config()
        if not cfg["enabled"]:
            self._auto_relogin_transition_ms = None
            self._auto_relogin_at = None
            return None
        try:
            status = self.client.connection_status()
        except Exception:
            return None
        if status.get("connected", True):
            self._auto_relogin_transition_ms = None
            self._auto_relogin_at = None
            return None
        transition_ms = status.get("changedAt")
        if (self._auto_relogin_at is None
                or transition_ms != self._auto_relogin_transition_ms
                or self._auto_relogin_delay_seconds != cfg["delaySeconds"]):
            self._auto_relogin_transition_ms = transition_ms
            self._auto_relogin_delay_seconds = cfg["delaySeconds"]
            base = (float(transition_ms) / 1000) if transition_ms else now
            self._auto_relogin_at = base + int(cfg["delaySeconds"])
        return max(0.0, self._auto_relogin_at - now)

    def _auto_relogin_if_due(self) -> bool:
        """Restore once under the account lock, then retry after the same delay."""
        from .updater import service as update_service
        delay = self._auto_relogin_delay()
        if delay is None or delay > 0 or self._stop.is_set() or update_service.is_installing():
            return False
        with self.lock:
            # A manual login, logout, or setting change may have happened while waiting.
            delay = self._auto_relogin_delay()
            if delay is None or delay > 0 or self._stop.is_set() or update_service.is_installing():
                return False
            cfg = config.get_auto_relogin_config()
            try:
                self.client.bootstrap()
                self.reset_task_runtime()
                self.note_state_refreshed()
                self.push_log({
                    "id": "auto_relogin", "name": "自动重登", "ok": True,
                    "detail": "连接已断开，等待后重新登录成功",
                }, "自动·")
                self._auto_relogin_transition_ms = None
                self._auto_relogin_at = None
                return True
            except Exception as error:
                self._auto_relogin_at = time.time() + int(cfg["delaySeconds"])
                self.push_log({
                    "id": "auto_relogin", "name": "自动重登", "ok": False,
                    "detail": f"重新登录失败：{error}，{cfg['delaySeconds']} 秒后重试",
                }, "自动·")
                return False

    def run_toggles(self, ids: list[str], prefix: str, log_to_buffer: bool = True) -> list[dict]:
        """在账号锁内执行一批 toggle 任务。log_to_buffer=True 时结果写日志缓冲（后台巡检用）；
        前台手动开关时设 False，由前端直接展示结果、避免与轮询重复。"""
        if not ids:
            return []
        # 更新安装开始后不再启动新的游戏请求；已经持锁的任务会自然跑完，更新器等待
        # 所有账号锁释放后才退出/覆盖程序。
        from .updater import service as update_service
        if self._stop.is_set() or update_service.is_installing():
            return []
        is_connected = getattr(self.client, "is_connected", None)
        if callable(is_connected) and not is_connected():
            return []
        # 分步任务每完成一步就写入缓冲，前端不必等整批请求结束才看到日志。
        on_partial = lambda result: self.push_log(result, prefix)
        with self.lock:
            if self._stop.is_set():
                return []
            ids = [task_id for task_id in ids
                   if task_id not in {"daily_achievement", "redeem_code", "send_meal", "month_signin"}
                   or self.toggles.get(task_id)]
            # 排队等锁期间可能已有另一轮失败；拿到锁后再次检查退避。
            now_ms = int(time.time() * 1000)
            ids = [task_id for task_id in ids if now_ms >= self._retry_at_ms(task_id)]
            if not ids:
                return []
            # 每次拿到账号锁后合并当前登录版本，任务内再检查，避免开关与后台重复执行。
            claim_runtime = self._claim_runtime()
            for task_id in ("send_meal", "month_signin"):
                if task_id in ids:
                    self.task_runtime.setdefault(task_id, {}).update(claim_runtime[task_id])
            if "redeem_code" in ids:
                self.params.setdefault("redeem_code", {})
            results = run_toggle_tasks(
                self.client, ids, self.params, self.task_runtime, on_partial=on_partial)
            for result in results:
                task_id = result.get("id")
                if task_id == "send_meal":
                    # 投递/邮件确认有各自餐次闸门，沿用任务内部的 5 分钟退避。
                    continue
                if task_id != "redeem_code":
                    runtime_state = self.task_runtime.setdefault(task_id, {})
                    failed = (not result.get("skipped")
                              and (result.get("retryable") or not result.get("ok")))
                    if failed and not result.get("retryManaged"):
                        runtime_state["retryAtMs"] = int(time.time() * 1000) + TASK_RETRY_MS
                        if task_id == "daily_achievement":
                            self._quest_check_pending = True
                    else:
                        runtime_state.pop("retryAtMs", None)
                    if task_id == "shop_free" and not failed:
                        self._shop_free_last = time.time()
                    continue
                runtime_state = self.task_runtime.setdefault("redeem_code", {})
                if not result.get("skipped"):
                    if result.get("retryable") or not result.get("ok"):
                        runtime_state["nextRetryAt"] = time.time() + TASK_RETRY_MS / 1000
                        self._redeem_pending = True
                    else:
                        runtime_state.pop("nextRetryAt", None)
                        self._redeem_checked_revision = redeem_codes.revision()
                        self._redeem_pending = False
                elif time.time() >= (runtime_state.get("nextRetryAt") or 0):
                    self._redeem_checked_revision = redeem_codes.revision()
                    self._redeem_pending = False
                config.save_account(self.email, toggles=self.toggles, params=self.params)
        # 免费秘密商店的装备卡片需要沿用本轮货架，供"仅显示"模式下的手动购买使用。
        # 货架只保存在账号运行态，不塞进日志/HTTP 返回，避免把整份购买记录重复传给前端。
        for result in results:
            if result.get("id") != "shop_free":
                continue
            shelf = result.pop("records", None)
            if isinstance(shelf, list):
                self.shop_shelf = shelf
        if log_to_buffer:
            for r in results:
                # 只记"确实向服务器发了请求"的结果：到点未满/未到时间而跳过(skipped)的
                # 不产生请求，不入日志，避免巡检刷屏。
                if r.get("skipped") or r.get("liveLogged"):
                    continue
                self.push_log(r, prefix)
        return results

    def run_due(self, prefix: str = "巡检·") -> list[dict]:
        """跑本轮到点该跑的开关（结果进日志缓冲）。"""
        ids = self._due_ids()
        quest_revision = self._quest_update_revision
        results = self.run_toggles(ids, prefix)
        achievement = next((r for r in results if r.get("id") == "daily_achievement"), None)
        if (achievement is not None and achievement.get("ok")
                and not achievement.get("retryable")
                and self._quest_update_revision == quest_revision):
            self._quest_checked_revision = quest_revision
            self._quest_check_pending = False
        return results

    def notify_config_changed(self):
        """开关/参数变了 → 立刻唤醒调度线程（不必等下一个到点时刻）。

        用户拨开竞技场开关后再勾 NPC，勾选本身不触发执行；没有这个唤醒，就要等
        下一轮巡检才动，看起来像"隔了几分钟才有日志"。
        """
        redeem_enabled = bool(self.toggles.get("redeem_code"))
        if redeem_enabled and not self._redeem_pending:
            self._redeem_pending = True
        elif not redeem_enabled:
            self._redeem_pending = False
        enabled = bool(self.toggles.get('daily_achievement'))
        options = achievement_options(self.params.get("daily_achievement"))
        if enabled and (not self._daily_achievement_enabled or options != self._achievement_options):
            self._quest_check_pending = True
        self._daily_achievement_enabled = enabled
        self._achievement_options = options
        meal_enabled = bool(self.toggles.get("send_meal"))
        if meal_enabled and not self._send_meal_enabled:
            self._queue_meal_check()
        self._send_meal_enabled = meal_enabled
        self._wake.set()

    def note_state_refreshed(self):
        """手动重登成功后重排每日任务，避免当天再次自动重登。"""
        self._sync_arena_after_login()
        self._auto_relogin_at = None
        self._auto_relogin_transition_ms = None
        self.state_revision += 1
        self.state_synced_at = int(time.time() * 1000)
        self._note_guild_login()
        self._queue_meal_check()
        (self.task_runtime.get("month_signin") or {}).pop("retryAtMs", None)
        self.state_sync_error = None
        self._state_refresh_at = _next_daily_state_refresh()
        self._on_quest_updates()
        self._wake.set()

    def _sync_arena_after_login(self):
        """只在成功登录后，用新快照恢复已取得具体冷却时间的 NPC。"""
        runtime = self.task_runtime.get("arena_npc") or {}
        waiting = runtime.get("waitingForLogin") or {}
        if not waiting:
            return
        snapshot_times = arena_next_times(self.client.account_state or {})
        for scene_id in list(waiting):
            if scene_id in snapshot_times:
                waiting.pop(scene_id, None)
                (runtime.get("nextMs") or {}).pop(scene_id, None)
                (runtime.get("retryMs") or {}).pop(scene_id, None)

    def _next_wake_delay(self) -> float:
        """按最近档期等待，最长 SCAN_MAX_GAP；已到期或缺档期时避免空转。

        数据源与前端面板同一套 `next_claims`（游戏档期与失败退避），所以
        调度时刻与界面上显示的倒计时天然一致。
        """
        ids = self._enabled_ids()
        now = time.time()
        auto_delay = self._auto_relogin_delay(now)
        if auto_delay is not None:
            from .updater import service as update_service
            if update_service.is_installing():
                return SCAN_MIN_GAP
            return min(SCAN_MAX_GAP, auto_delay)
        now_ms = now * 1000
        try:
            nc = self.next_claims(int(now_ms))
        except Exception:
            nc = {}
        if "redeem_code" in ids:
            runtime = self.task_runtime.get("redeem_code") or {}
            retry_at = runtime.get("nextRetryAt") or 0
            if self._redeem_pending and now >= retry_at:
                return 0.0
        if ('daily_achievement' in ids and
                any(achievement_options(self.params.get("daily_achievement")).values()) and
                (self._quest_check_pending or
                 self._quest_update_revision > self._quest_checked_revision) and
                now_ms >= self._retry_at_ms("daily_achievement")):
            return 0.0
        # 每日快照刷新是账号级调度事件，即使没有开启任何任务也必须参与唤醒计算。
        cands: list[float] = [max(0.0, self._state_refresh_at - now), redeem_codes.next_refresh_delay()]
        guild_delay = self._guild_due_delay()
        if guild_delay is not None:
            cands.append(guild_delay)
        for tid in ids:
            if tid == "redeem_code":
                if self._redeem_pending:
                    retry_at = (self.task_runtime.get(tid) or {}).get("nextRetryAt") or 0
                    cands.append(max(0.0, retry_at - now))
                continue
            if tid == "daily_achievement":
                if (any(achievement_options(self.params.get(tid)).values()) and
                        (self._quest_check_pending or
                         self._quest_update_revision > self._quest_checked_revision)):
                    cands.append(max(0.0, (self._retry_at_ms(tid) - now_ms) / 1000))
                continue
            if tid == "arena_npc":
                cands.append(max(self._arena_due_delay(now_ms),
                                 (self._retry_at_ms(tid) - now_ms) / 1000))
                continue
            e = nc.get(tid) or {}
            if e.get("blocked"):
                continue                      # 条件不满足（如幻境层数不够）：不排期
            if e.get("retrying"):
                cands.append(max(0.0, (e["nextMs"] - now_ms) / 1000))
                continue
            if TASKS.get(tid, {}).get("needsFreshState"):
                # 判定依赖每日重置字段，统一排到当天随机生成的快照刷新时刻。
                cands.append(max(0.0, self._state_refresh_at - now))
                continue
            nxt = _next_action_ms(e)
            cands.append(0.0 if nxt is None else max(0.0, (nxt - now_ms) / 1000))
        if not cands:
            return SCAN_MAX_GAP
        # 未到期的档期按剩余时间等待：缓存复查后商店可能只剩 1 秒，不能再等 1 分钟。
        return min(SCAN_MAX_GAP, min(delay if delay > 0 else SCAN_MIN_GAP for delay in cands))

    def next_fixed_task_at_ms(self, now_ms: int | None = None) -> int | None:
        """Return the earliest real deadline for an enabled scheduled task.

        The scheduler caps its polling delay at ``SCAN_MAX_GAP`` and uses
        ``SCAN_MIN_GAP`` to avoid spinning on overdue or unknown deadlines.
        Those polling limits are useful for recovery,
        but it is not the task's own schedule and therefore cannot describe
        the application clock in the background restriction test.  This
        read-only projection uses the same state fields as the task gates and
        leaves the polling policy out of the result.

        ``None`` means that this account has no enabled task with a fixed
        deadline.  Event-driven tasks (兑换码 and成就) are intentionally not
        included because they do not provide a timestamp to schedule against.
        """
        state = self.client.account_state or {}
        if not isinstance(state, dict):
            return None
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        enabled = self._enabled_ids()
        if not enabled:
            return None

        candidates: list[int] = []
        try:
            claim_times = self.next_claims(now)
        except Exception:
            claim_times = {}

        for task_id in enabled:
            # These tasks are triggered by a state update or an external
            # catalogue change rather than by a clock deadline.
            if task_id in {"redeem_code", "daily_achievement"}:
                continue

            if task_id == "arena_npc":
                picked = (self.params.get("arena_npc") or {}).get("picked") or []
                if not picked:
                    continue
                snapshot_times = arena_next_times(state)
                runtime = self.task_runtime.get("arena_npc") or {}
                next_times = runtime.get("nextMs") or {}
                retry_times = runtime.get("retryMs") or {}
                waiting = runtime.get("waitingForLogin") or {}
                for value in picked:
                    try:
                        scene_id = f"HellNPC_{int(value)}"
                    except (TypeError, ValueError):
                        continue
                    if waiting.get(scene_id):
                        continue
                    gate = max(
                        int(snapshot_times.get(scene_id) or 0),
                        int(next_times.get(scene_id) or 0),
                        int(retry_times.get(scene_id) or 0),
                        self._retry_at_ms(task_id),
                    )
                    candidates.append(max(now, gate))
                continue

            entry = claim_times.get(task_id)
            if not isinstance(entry, dict) or entry.get("blocked"):
                continue
            if entry.get("retrying"):
                candidates.append(max(now, int(entry["nextMs"])))
                continue
            if TASKS.get(task_id, {}).get("needsFreshState"):
                # An already available daily task runs immediately.  Once its
                # quota is consumed, the next meaningful clock event is the
                # planned post-reset snapshot refresh.
                if entry.get("ready"):
                    candidates.append(now)
                else:
                    candidates.append(max(now, int(self._state_refresh_at * 1000)))
                continue

            next_ms = _next_action_ms(entry)
            if next_ms is None:
                # Missing game-side timestamps mean the task is immediately
                # eligible (the task itself performs the final gate check).
                if entry.get("ready"):
                    candidates.append(now)
                continue
            try:
                candidates.append(max(now, int(float(next_ms))))
            except (TypeError, ValueError, OverflowError):
                continue

        return min(candidates) if candidates else None

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
        waiting = rt.get("waitingForLogin") or {}
        snap_map = arena_next_times(self.client.account_state or {})
        best = None
        for n in picked:
            sid = f"HellNPC_{int(n)}"
            if waiting.get(sid):
                continue
            gate = max(snap_map.get(sid) or 0, next_map.get(sid) or 0,
                       retry_map.get(sid) or 0)
            delay = 0.0 if not gate else max(0.0, (gate - now_ms) / 1000)
            best = delay if best is None else min(best, delay)
            if best == 0.0:
                break
        return SCAN_MAX_GAP if best is None else best

    def reset_task_runtime(self):
        """重置任务运行态，保留体力邮件确认和等待登录同步的 NPC。

        手动刷新和断线自动重登在成功 bootstrap 后调用。竞技场普通退避和本地冷却
        交给新登录快照替代；暂停标记保留至 note_state_refreshed 核对具体冷却时间。
        """
        # 体力投递与邮件确认是两个请求；刷新不能抹掉中途的确认状态或重试档期。
        meal_runtime = self.task_runtime.get("send_meal")
        arena_waiting = (self.task_runtime.get("arena_npc") or {}).get("waitingForLogin")
        self.task_runtime.clear()
        if meal_runtime is not None:
            self.task_runtime["send_meal"] = meal_runtime
        if arena_waiting:
            self.task_runtime["arena_npc"] = {"waitingForLogin": dict(arena_waiting)}

    def _run_loop(self):
        if not self._stop.is_set():
            _log.info("[scheduler] account=%s started %s", self.email,
                      _scheduler_runtime_status())
        first_pass = not self._startup_done
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if not self._startup_done:
                    self._startup_done = True
                    self._run_startup_tasks()
                if self._stop.is_set():
                    break
                self._auto_relogin_if_due()
                self._refresh_state_if_due()
                redeem_codes.refresh_if_due()
                if self._stop.is_set():
                    break
                if redeem_codes.revision() > self._redeem_checked_revision:
                    self._redeem_pending = bool(self.toggles.get("redeem_code"))
                # 登录后的每日签到、每日体力等自动任务先执行；佣兵团进入流程放在
                # 本轮任务之后，避免它抢在这些登录任务之前发送请求。
                self.run_due("登录·" if first_pass else "巡检·")
                self._run_guild_if_due()
            except Exception as e:
                _log.exception("账号 %s 后台巡检异常: %s", self.email, e)
                self.push_log({"id": "automation", "name": "自动任务", "ok": False,
                               "detail": str(e)}, "自动·")
            first_pass = False
            if self._stop.is_set():
                break
            # 睡到"最早一个到点时刻"，而不是固定 5 分钟盲扫。
            # 期间用户改开关/改勾选会置位 _wake 提前唤醒（notify_config_changed）。
            delay = self._next_wake_delay()
            wait_started = time.time()
            planned_at = wait_started + delay
            _log.info(
                "[scheduler] account=%s waiting delay=%.3fs plannedWake=%s enabled=%s %s",
                self.email, delay, _scheduler_time(planned_at),
                ",".join(self._enabled_ids()) or "none", _scheduler_runtime_status(),
            )
            woken = self._wake.wait(delay)
            actual_at = time.time()
            if not self._stop.is_set():
                _log.info(
                    "[scheduler] account=%s woke reason=%s actual=%s planned=%s "
                    "waited=%.3fs lateness=%.3fs %s",
                    self.email, "config-change" if woken else "timer",
                    _scheduler_time(actual_at), _scheduler_time(planned_at),
                    actual_at - wait_started,
                    max(0.0, actual_at - planned_at) if not woken else 0.0,
                    _scheduler_runtime_status(),
                )

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
    """登记已认证的会话，等待主界面启动任务；锁外关闭旧会话。"""
    acc = Account(email, client, portal)
    with _reg_lock:
        old = _accounts.get(email)
        _accounts[email] = acc
    # A running task may hold old.lock while checking accounts.get().
    if old:
        old.close()
    return acc


def remove(email: str) -> bool:
    """下线并从注册表移除（不删存档，存档另由 config.delete_account 处理）。"""
    with _reg_lock:
        acc = _accounts.pop(email, None)
    if acc:
        acc.close()
        return True
    return False
