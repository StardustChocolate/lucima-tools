"""Cancelable guild jobs using the application's existing account sessions.

The journal and request-ID deduplication are process-local. Jobs are never resumed
or replayed after a process restart: a dropped connection may already have
claimed rewards on the game server.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
import random
import threading
import uuid

from . import accounts, config
from .game_client import GameError, GameMessage
from .guild import GuildService, _nonnegative_int, describe_error, has_guild, is_auth_error, object_id, positive_int, quest_response_summary
from .guild_quests import GuildWeeklyService, weekly_catalog


_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ACTIVE = {"running", "stopping"}


class _Cancelled(Exception):
    pass


class GuildJobManager:
    def __init__(self, *, service_factory=GuildService, account_lookup=None, interval=None):
        self._service_factory = service_factory
        self._account_lookup = account_lookup or accounts.get
        self._interval = interval
        self._lock = threading.RLock()
        self._jobs = {}
        self._latest = {}
        self._active = {}
        self._requests = {}
        self._cancel_events = {}

    @staticmethod
    def _email(account):
        return account if isinstance(account, str) else account.email

    @staticmethod
    def _empty(account, job_id=None, kind="quest_rewards"):
        result = {"id": job_id, "kind": kind, "state": "unknown" if job_id else "idle", "running": False,
                  "account": account, "requested": 0, "completed": 0,
                  "message": "任务不存在或应用已经重启" if job_id else "等待操作", "logs": []}
        if kind == "quest_rewards":
            result.update(rounds=0, round=0, total=0, attempted=0, succeeded=0, failed=0, noDetails=0,
                          guildName="", questIds=[], results=[])
        return result

    def _snapshot(self, job):
        return deepcopy(job)

    def _message(self, job, message, *, state=None):
        with self._lock:
            if state is not None:
                job["state"] = state
                job["running"] = state in _ACTIVE
            job["message"] = message
            job["logs"].append({"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "message": message})
            del job["logs"][:-100]

    def start_rewards(self, acc, rounds=30, requestId=None):
        rounds = positive_int(rounds, "领取轮数", 100)
        return self._register(acc, "quest_rewards", {
            "rounds": rounds, "round": 0, "requested": 0, "total": 0,
            "attempted": 0, "completed": 0, "succeeded": 0, "failed": 0, "noDetails": 0,
            "guildName": "", "questIds": [], "results": [],
        }, self._run_rewards, requestId)

    def start_score(self, acc, requestId=None):
        return self._register(acc, "quest_rewards", {
            "mode": "score", "score": None, "target": max(row["target"] for row in weekly_catalog()["tiers"].values()),
            "attempted": 0, "completed": 0, "planned": 0, "pointsPerClaim": None,
            "phase": "preparing", "questIds": [],
        }, self._run_score, requestId)

    def _register(self, acc, kind, fields, runner, request_id):
        if request_id is not None:
            if not isinstance(request_id, str) or not _UUID.fullmatch(request_id):
                raise GameError("请求标识必须是 UUID")
            request_id = request_id.lower()
        email = acc.email
        with self._lock:
            if self._account_lookup(email) is not acc:
                raise GameError("当前登录会话已变更，请使用当前账号重新操作")
            if request_id is not None and (email, request_id) in self._requests:
                previous = self._jobs.get((email, self._requests[(email, request_id)]))
                if previous is None:
                    return self._empty(email, request_id, kind)
                if previous["kind"] != kind:
                    raise GameError("请求标识已用于另一类佣兵团任务，请重新操作")
                return self._snapshot(previous)
            active = self._jobs.get((email, self._active.get(email)))
            if active and active["state"] in _ACTIVE:
                raise GameError("该账号已有佣兵团任务，请等待完成或先停止")
            # The caller retains this ID even if the HTTP start response is lost.
            job_id = request_id or str(uuid.uuid4())
            job = {"id": job_id, "kind": kind, "state": "running", "running": True,
                   "account": email, "message": "", "logs": [], **fields}
            event = threading.Event()
            self._jobs[(email, job_id)] = job
            self._latest[(email, kind)] = job_id
            self._active[email] = job_id
            self._cancel_events[(email, job_id)] = event
            if request_id is not None:
                self._requests[(email, request_id)] = job_id
            self._message(job, "任务已开始，正在等待账号空闲")
            thread = threading.Thread(target=runner, args=(acc, job, event),
                                      name=f"guild-{job_id[:8]}", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                self._message(job, "任务未能启动，请重新操作", state="failed")
                self._release_active(job)
                self._cancel_events.pop((email, job_id), None)
                raise GameError("任务未能启动，请重新操作") from None
            return self._snapshot(job)

    def status(self, account, id=None, kind="quest_rewards"):
        email = self._email(account)
        with self._lock:
            job = self._jobs.get((email, id or self._latest.get((email, kind))))
            if job is None or job["account"] != email or job["kind"] != kind:
                return self._empty(email, id, kind)
            return self._snapshot(job)

    def cancel(self, account, id=None, kind="quest_rewards"):
        email = self._email(account)
        with self._lock:
            job = self._jobs.get((email, id or self._latest.get((email, kind))))
            if job is None or job["account"] != email or job["kind"] != kind:
                return self._empty(email, id, kind)
            if job["state"] in _ACTIVE:
                self._cancel_events[(email, job["id"])].set()
                if job["state"] != "stopping":
                    self._message(job, "正在停止；已发送的请求需等待返回，不会发送下一笔", state="stopping")
            return self._snapshot(job)

    def _release_active(self, job):
        with self._lock:
            if self._active.get(job["account"]) == job["id"]:
                self._active.pop(job["account"], None)
            self._cancel_events.pop((job["account"], job["id"]), None)
            finished = [key for key, value in self._jobs.items()
                        if key[0] == job["account"] and value["state"] not in _ACTIVE]
            for key in finished[:-10]:
                self._jobs.pop(key, None)

    def _check(self, acc, event):
        if event.is_set():
            raise _Cancelled()
        if self._account_lookup(acc.email) is not acc:
            raise GameError("当前账号已退出或重新登录，任务已停止；请查询游戏中的实际结果")
        if not acc.client.is_connected():
            raise GameError("当前游戏连接已断开，任务已停止")

    def _wait(self, event):
        if self._interval is not None:
            seconds = self._interval
        else:
            delay = config.get_request_delay_config()
            jitter = delay["jitter"] / 100
            seconds = delay["ms"] / 1000 * random.uniform(1 - jitter, 1 + jitter) if delay["enabled"] else 0
        if event.wait(seconds):
            raise _Cancelled()

    def _invoke(self, acc, event, method, *args):
        while True:
            self._check(acc, event)
            if acc.lock.acquire(timeout=0.2):
                break
        try:
            self._check(acc, event)
            return method(*args)
        finally:
            acc.lock.release()

    def _safe_error(self, acc, error):
        # Do not include HTTP exceptions, request bodies or tracebacks in progress.
        if not isinstance(error, (GameError, GameMessage)):
            return "请求失败或连接中断，结果未确认；请查询佣兵团任务和奖励记录后再操作，任务不会自动重试"
        message = describe_error(error)
        if isinstance(error, GameMessage) and str(error) == "OverCount":
            message = "已达到游戏服务器允许的领取次数或数量上限（OverCount）"
        for attr in ("session_id", "aid"):
            value = getattr(acc.client, attr, None)
            if value is not None and len(str(value)) >= 6:
                message = message.replace(str(value), "[已隐藏]")
        return message

    def _run_score(self, acc, job, event):
        revision = acc.state_revision
        runtime = acc.guild_runtime
        gid, week = None, None
        pending_claim = None

        def check_session():
            from .updater import service as update_service
            self._check(acc, event)
            if acc._stop.is_set() or update_service.is_installing() or acc.state_revision != revision:
                raise GameError("账号会话已刷新或应用正在退出，任务已停止")
            if not has_guild(acc.client.account_state):
                raise GameError("当前账号尚未加入佣兵团，任务已停止")

        def snapshot():
            check_session()
            current_gid = object_id(acc.client.account_state["PlayerGuildInfo"].get("GID"))
            status = weekly.snapshot()
            if gid is not None and (gid != current_gid or week != status["week"]):
                raise GameError("佣兵团或周任务周期已变化，任务已停止")
            if status["week"] is None or status["score"] is None:
                raise GameError("全团积分或本周资料尚未确认，请刷新佣兵团后再试")
            return status

        def completed_tasks(status):
            quests = {row.get("StaticID"): row for row in
                      (acc.client.account_state.get("QuestContainer") or {}).get("Quests") or []
                      if isinstance(row, dict)}
            return [row["id"] for row in status["personal"]
                    if row["state"] in {"ready", "claimed"} and row["target"] is not None
                    and row["count"] is not None and row["count"] >= row["target"]
                    and quests.get(row["id"], {}).get("IsFinish") is True]

        def initialize():
            nonlocal gid, week
            check_session()
            runtime["weeklyScoreJob"] = job["id"]
            service.query()
            status = snapshot()
            gid = object_id(acc.client.account_state["PlayerGuildInfo"].get("GID"))
            week = status["week"]
            return status

        def claim(identifier):
            nonlocal pending_claim
            before = snapshot()
            if before["score"] >= job["target"]:
                return before
            if identifier not in completed_tasks(before):
                raise GameError("已完成周任务的资料已变化，请刷新后再试")
            pending = runtime.setdefault("weeklyUncertain", [])
            # An ordinary first claim must remain blocked if its reply is lost.
            if any(row["id"] == identifier and row["state"] == "ready" for row in before["personal"]):
                pending.append(identifier)
            with self._lock:
                job["attempted"] += 1
            pending_claim = identifier
            response = service.claim_quest(identifier)
            parts = [response] + [part for part in response.get("Results") or [] if isinstance(part, dict)]
            if any(row.get("Week") != week or object_id(row.get("GID")) != gid
                   for part in parts for row in part.get("GuildQuestDataList") or []):
                raise GameError("领奖响应中的佣兵团或周任务周期已变化，任务已停止")
            after = weekly.snapshot()
            if any(row["id"] == identifier and row["state"] == "claimed" for row in after["personal"]):
                if identifier in pending:
                    pending.remove(identifier)
            with self._lock:
                job["completed"] += 1
            pending_claim = None
            return after

        def update_score(status):
            with self._lock:
                job["score"] = status["score"]
            acc._guild_revision += 1

        try:
            service = self._service_factory(acc.client, runtime)
            weekly = GuildWeeklyService(service)
            self._message(job, "正在确认本周任务与全团积分")
            status = self._invoke(acc, event, initialize)
            update_score(status)
            planned, identifier = 0, None
            if status["score"] < job["target"]:
                identifiers = completed_tasks(status)
                if not identifiers:
                    raise GameError("当前没有已确认完成的周任务，请完成任务或重新登录同步个人进度")
                catalog = weekly_catalog()["personal"]
                candidates = [(item, _nonnegative_int(catalog.get(item, {}).get("points")))
                              for item in identifiers]
                candidates = [(item, points) for item, points in candidates if points]
                if not candidates:
                    raise GameError("已完成任务缺少固定积分配置，请更新应用资源后再试")
                identifier, points = max(candidates, key=lambda item: item[1])
                planned = (job["target"] - status["score"] + points - 1) // points
                with self._lock:
                    job.update(questIds=[identifier], planned=planned, pointsPerClaim=points, phase="claiming")
                self._message(job, f"每次 {points} 分，预计领取 {planned} 次后校验积分")
            for _ in range(planned):
                self._wait(event)
                status = self._invoke(acc, event, claim, identifier)
                update_score(status)
                self._message(job, f"已领取 {job['completed']}/{planned} 次")
                # Other members or a complete reward reply can confirm the target
                # early. Never put estimated points into the shared guild cache.
                if status["score"] >= job["target"]:
                    break
            if job["completed"]:
                with self._lock:
                    job["phase"] = "verifying"
                self._message(job, "正在查询并校验实际全团积分")
                self._wait(event)
            def finish():
                snapshot()
                if job["completed"]:
                    service.query()
                current = snapshot()
                update_score(current)
                if current["score"] < job["target"]:
                    raise GameError(f"校验后全团积分 {current['score']}/{job['target']}，尚未达到目标，已停止；请检查实际结果")
                with self._lock:
                    self._check(acc, event)
                    job["score"] = current["score"]
                    self._message(job, f"全团积分 {job['score']}/{job['target']}，已满足全部积分奖励条件", state="completed")
            self._invoke(acc, event, finish)
        except _Cancelled:
            self._message(job, "已停止，不再发送后续领取请求", state="cancelled")
        except Exception as error:
            prefix = f"任务 {pending_claim} 的领取结果未确认；" if pending_claim else ""
            self._message(job, prefix + self._safe_error(acc, error), state="failed")
        finally:
            # The marker only suppresses ordinary weekly claims while this job
            # owns them. A relogin may already have replaced the runtime state.
            if runtime.get("weeklyScoreJob") == job["id"]:
                runtime.pop("weeklyScoreJob", None)
            acc._guild_revision += 1
            acc._wake.set()
            acc.push_log({"id": "guild_repeat_weekly", "name": "获取团积分",
                          "ok": job["state"] == "completed", "detail": job["message"]}, "主动·")
            self._release_active(job)

    def _run_rewards(self, acc, job, event):
        pending_claim = None

        def claim(quest_id):
            nonlocal pending_claim
            with self._lock:
                self._check(acc, event)
                job["attempted"] += 1
            pending_claim = (job["round"], quest_id)
            return service.claim_quest(quest_id)

        try:
            self._check(acc, event)
            service = self._service_factory(acc.client)
            self._message(job, "正在查询佣兵团任务")
            listing = self._invoke(acc, event, service.quest_ids)
            if not isinstance(listing, dict) or not isinstance(listing.get("questIds"), list):
                raise GameError("佣兵团任务列表格式无效，未继续发送领取请求")
            quest_ids = listing["questIds"]
            if any(not isinstance(quest_id, str) or not quest_id.strip() for quest_id in quest_ids):
                raise GameError("佣兵团任务列表包含无效任务 ID，未继续发送领取请求")
            with self._lock:
                job["guildName"] = str(listing.get("guildName") or "")
                job["questIds"] = list(quest_ids)
                job["total"] = job["requested"] = len(quest_ids) * job["rounds"]
            self._check(acc, event)
            if not quest_ids:
                with self._lock:
                    self._check(acc, event)
                    self._message(job, "当前佣兵团没有可提交的任务 ID，未发送任何领取请求", state="completed")
                return
            # Match the reference tool: snapshot IDs once, then process that
            # ordered list once per configured round. Each round is intentional;
            # transport failures never trigger an automatic retry.
            for round_number in range(1, job["rounds"] + 1):
                self._check(acc, event)
                with self._lock:
                    job["round"] = round_number
                for index, quest_id in enumerate(quest_ids):
                    self._check(acc, event)
                    self._message(job, f"第 {round_number}/{job['rounds']} 轮：领取任务 {index + 1}/{len(quest_ids)}（{quest_id}）")
                    try:
                        response = self._invoke(acc, event, claim, quest_id)
                        # The reference accepts empty optional reward fields.
                        # Record receipt without asserting a reward gain.
                        summary = quest_response_summary(response)
                    except GameMessage as error:
                        if is_auth_error(error) or not acc.client.is_connected():
                            raise
                        state = "rejected"
                        message = self._safe_error(acc, error)
                        response_fields = ""
                    else:
                        state = summary["state"]
                        message = summary["message"]
                        response_fields = summary["responseFields"]
                    with self._lock:
                        if state == "success":
                            job["succeeded"] += 1
                        elif state == "received":
                            job["noDetails"] += 1
                        else:
                            job["failed"] += 1
                        job["completed"] += 1
                        job["results"].append({"round": round_number, "questId": quest_id,
                                               "state": state, "message": message,
                                               "responseFields": response_fields})
                        del job["results"][:-200]
                    pending_claim = None
                    self._message(job, f"任务 {quest_id}：{message or '已处理'}")
                    self._check(acc, event)
                    if index + 1 < len(quest_ids) or round_number < job["rounds"]:
                        self._wait(event)
            with self._lock:
                self._check(acc, event)
                self._message(job, f"已完成 {job['rounds']} 轮，返回数据 {job['succeeded']} 项，业务拒绝 {job['failed']} 项，无明细 {job['noDetails']} 项；实际奖励以游戏记录为准",
                              state="completed")
        except _Cancelled:
            self._message(job, f"已停止：返回数据 {job['succeeded']} 项，业务拒绝 {job['failed']} 项，无明细 {job['noDetails']} 项；未再发送后续请求",
                          state="cancelled")
        except Exception as error:
            message = self._safe_error(acc, error)
            if pending_claim is not None:
                pending_round, pending_id = pending_claim
                message = f"第 {pending_round} 轮 · 任务 {pending_id}：结果未确认。{message}"
            self._message(job, f"{message}（返回数据 {job['succeeded']} 项，业务拒绝 {job['failed']} 项，无明细 {job['noDetails']} 项；已停止，不会自动重试）",
                          state="failed")
        finally:
            self._release_active(job)


manager = GuildJobManager()
