"""Cancelable guild jobs using the application's existing account sessions.

The journal and request-ID deduplication are process-local. Jobs are never resumed
or replayed after a process restart: a dropped connection may already have spent
gold or claimed rewards on the game server.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
import threading
import uuid

from . import accounts
from .game_client import GameClient, GameError, GameMessage
from .guild import (GuildService, describe_error, guild_identity, object_id, positive_int,
                    quest_response_summary)


_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ACTIVE = {"running", "stopping"}


class _Cancelled(Exception):
    pass


class GuildJobManager:
    def __init__(self, *, service_factory=GuildService, account_lookup=None, interval=0.5):
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
    def _empty(account, job_id=None, kind="donation"):
        result = {"id": job_id, "kind": kind, "state": "unknown" if job_id else "idle", "running": False,
                  "account": account, "target": "", "requested": 0, "completed": 0,
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

    def start(self, acc, target="", maxDonations=1, leaveAfter=False, requestId=None):
        requested = positive_int(maxDonations, "捐献次数", 100)
        if not isinstance(leaveAfter, bool):
            raise GameError("完成后退会必须是布尔值")
        if target is None:
            target = ""
        if not isinstance(target, str) or len(target.strip()) > 120:
            raise GameError("目标工会名称无效")
        target = target.strip()
        return self._register(acc, "donation", {
            "target": target, "requested": requested, "completed": 0, "leaveAfter": leaveAfter,
        }, self._run, requestId)

    def start_rewards(self, acc, rounds=30, requestId=None):
        rounds = positive_int(rounds, "领取轮数", 100)
        return self._register(acc, "quest_rewards", {
            "rounds": rounds, "round": 0, "requested": 0, "total": 0,
            "attempted": 0, "completed": 0, "succeeded": 0, "failed": 0, "noDetails": 0,
            "guildName": "", "questIds": [], "results": [],
        }, self._run_rewards, requestId)

    def _register(self, acc, kind, fields, runner, request_id):
        if request_id is not None:
            if not isinstance(request_id, str) or not _UUID.fullmatch(request_id):
                raise GameError("请求标识必须是 UUID")
            request_id = request_id.lower()
        email = acc.email
        with self._lock:
            if request_id is not None and (email, request_id) in self._requests:
                previous = self._jobs[(email, self._requests[(email, request_id)])]
                if previous["kind"] != kind:
                    raise GameError("请求标识已用于另一类工会任务，请重新操作")
                return self._snapshot(previous)
            active = self._jobs.get((email, self._active.get(email)))
            if active and active["state"] in _ACTIVE:
                raise GameError("该账号已有工会任务，请等待完成或先停止")
            if self._account_lookup(email) is not acc:
                raise GameError("当前登录会话已变更，请使用当前账号重新操作")
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
            thread.start()
            return self._snapshot(job)

    def status(self, account, id=None, kind="donation"):
        email = self._email(account)
        with self._lock:
            job = self._jobs.get((email, id or self._latest.get((email, kind))))
            if job is None or job["account"] != email or job["kind"] != kind:
                return self._empty(email, id, kind)
            return self._snapshot(job)

    def cancel(self, account, id=None, kind="donation"):
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

    def _check(self, acc, event):
        if event.is_set():
            raise _Cancelled()
        if self._account_lookup(acc.email) is not acc:
            raise GameError("当前账号已退出或重新登录，任务已停止；请查询游戏中的实际结果")

    def _invoke(self, acc, event, method, *args):
        self._check(acc, event)
        return method(*args)

    def _safe_error(self, acc, error, kind="donation"):
        # Do not include HTTP exceptions, request bodies or tracebacks in progress.
        if not isinstance(error, (GameError, GameMessage)):
            if kind == "quest_rewards":
                return "请求失败或连接中断，结果未确认；请查询工会任务和奖励记录后再操作，任务不会自动重试"
            return "请求失败或连接中断，结果未确认；请查询工会和金币余额后再操作，任务不会自动重试"
        message = describe_error(error)
        if kind == "quest_rewards" and isinstance(error, GameMessage) and str(error) == "OverCount":
            message = "已达到游戏服务器允许的领取次数或数量上限（OverCount）"
        for attr in ("session_id", "aid"):
            value = getattr(acc.client, attr, None)
            if value is not None and len(str(value)) >= 6:
                message = message.replace(str(value), "[已隐藏]")
        return message

    def _run(self, acc, job, event):
        acquired = False
        try:
            # Keep this account's guild state stable throughout the flow. Status
            # and cancel use only the manager lock, so they remain responsive.
            while not acquired:
                self._check(acc, event)
                acquired = acc.lock.acquire(timeout=0.2)
            self._check(acc, event)
            service = self._service_factory(acc.client)
            self._message(job, "正在查询当前工会")
            current = guild_identity(self._invoke(acc, event, service.query))
            target = job["target"]
            if current is not None:
                if target and current["name"] != target:
                    raise GameError(f"当前工会为「{current['name']}」，与目标「{target}」不一致，已停止；不会自动退会")
                if not target:
                    target = current["name"]
                    with self._lock:
                        job["target"] = target
            else:
                if not target:
                    raise GameError("当前未加入工会，请填写目标工会名称后再操作")
                self._message(job, f"正在查找目标工会「{target}」")
                result = self._invoke(acc, event, service.search, target)
                matches = []
                for candidate in result.get("GuildDatas") or []:
                    if not isinstance(candidate, dict):
                        continue
                    info = candidate.get("Info")
                    if isinstance(info, dict) and info.get("Name") == target:
                        matches.append(info)
                if len(matches) != 1:
                    raise GameError("未找到唯一同名目标工会，已停止；请核对完整工会名称")
                guild_id = object_id(matches[0].get("_id"))
                if not guild_id:
                    raise GameError("目标工会缺少有效 ID，已停止")
                self._message(job, f"正在加入工会「{target}」")
                self._invoke(acc, event, service.join, guild_id)
                current = guild_identity(self._invoke(acc, event, service.query))
                if current is None or current["name"] != target or current["id"] != guild_id:
                    raise GameError("尚未确认加入目标工会，可能需要审批或处于冷却期；未发送捐献请求")
            for index in range(job["requested"]):
                self._check(acc, event)
                self._message(job, f"正在捐献第 {index + 1}/{job['requested']} 次，每次 50,000 金币")
                self._invoke(acc, event, service.donate, 50000)
                with self._lock:
                    job["completed"] += 1
                self._message(job, f"已确认捐献 {job['completed']}/{job['requested']} 次")
                self._check(acc, event)
                if index + 1 < job["requested"] and event.wait(self._interval):
                    raise _Cancelled()
            if job["leaveAfter"]:
                self._message(job, "全部捐献完成，正在按设置退出工会")
                self._invoke(acc, event, service.leave)
            self._check(acc, event)
            with self._lock:
                # Serialize completion with cancel so a stop cannot be lost.
                self._check(acc, event)
                suffix = "，已退出工会" if job["leaveAfter"] else ""
                self._message(job, f"已完成 {job['completed']} 次捐献，共 {job['completed'] * 50000:,} 金币{suffix}",
                              state="completed")
        except _Cancelled:
            self._message(job, f"已停止，已确认成功 {job['completed']}/{job['requested']} 次；未再发送后续请求",
                          state="cancelled")
        except Exception as error:
            message = self._safe_error(acc, error)
            self._message(job, f"{message}（已确认成功 {job['completed']}/{job['requested']} 次）", state="failed")
        finally:
            if acquired:
                acc.lock.release()
            self._release_active(job)

    @staticmethod
    def _is_auth_error(error):
        if GameClient._looks_like_auth_failure(error):
            return True
        return any(marker in str(error).lower() for marker in (
            "expired", "session", "not login", "not logged", "login required",
            "duplicate login", "duplicatelogin", "重复登录",
            "未登录", "登录失效", "登录过期", "会话失效", "凭证失效",
        ))

    def _run_rewards(self, acc, job, event):
        acquired = False
        pending_claim = None
        try:
            while not acquired:
                self._check(acc, event)
                acquired = acc.lock.acquire(timeout=0.2)
            self._check(acc, event)
            service = self._service_factory(acc.client)
            self._message(job, "正在查询工会任务")
            listing = self._invoke(acc, event, service.quest_ids)
            if not isinstance(listing, dict) or not isinstance(listing.get("questIds"), list):
                raise GameError("工会任务列表格式无效，未继续发送领取请求")
            quest_ids = listing["questIds"]
            if any(not isinstance(quest_id, str) or not quest_id.strip() for quest_id in quest_ids):
                raise GameError("工会任务列表包含无效任务 ID，未继续发送领取请求")
            with self._lock:
                job["guildName"] = str(listing.get("guildName") or "")
                job["questIds"] = list(quest_ids)
                job["total"] = job["requested"] = len(quest_ids) * job["rounds"]
            self._check(acc, event)
            if not quest_ids:
                with self._lock:
                    self._check(acc, event)
                    self._message(job, "当前工会没有可提交的任务 ID，未发送任何领取请求", state="completed")
                return
            # Match the original client: snapshot IDs once, then process that
            # ordered list once per configured round. Each round is intentional;
            # transport failures never trigger an automatic retry.
            for round_number in range(1, job["rounds"] + 1):
                self._check(acc, event)
                with self._lock:
                    job["round"] = round_number
                for index, quest_id in enumerate(quest_ids):
                    self._check(acc, event)
                    self._message(job, f"第 {round_number}/{job['rounds']} 轮：领取任务 {index + 1}/{len(quest_ids)}（{quest_id}）")
                    with self._lock:
                        self._check(acc, event)
                        job["attempted"] += 1
                    pending_claim = (round_number, quest_id)
                    try:
                        response = service.claim_quest(quest_id)
                        # Empty optional reward fields are normal in the original
                        # client. Record them as received without asserting reward gain.
                        summary = quest_response_summary(response)
                    except GameMessage as error:
                        if self._is_auth_error(error):
                            raise
                        state = "rejected"
                        message = self._safe_error(acc, error, "quest_rewards")
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
                    pending_claim = None
                    self._message(job, f"任务 {quest_id}：{message or '已处理'}")
                    self._check(acc, event)
                    if (index + 1 < len(quest_ids) or round_number < job["rounds"]) and event.wait(self._interval):
                        raise _Cancelled()
            with self._lock:
                self._check(acc, event)
                self._message(job, f"已完成 {job['rounds']} 轮，返回数据 {job['succeeded']} 项，业务拒绝 {job['failed']} 项，无明细 {job['noDetails']} 项；实际奖励以游戏记录为准",
                              state="completed")
        except _Cancelled:
            self._message(job, f"已停止：返回数据 {job['succeeded']} 项，业务拒绝 {job['failed']} 项，无明细 {job['noDetails']} 项；未再发送后续请求",
                          state="cancelled")
        except Exception as error:
            message = self._safe_error(acc, error, "quest_rewards")
            if pending_claim is not None:
                pending_round, pending_id = pending_claim
                message = f"第 {pending_round} 轮 · 任务 {pending_id}：结果未确认。{message}"
            self._message(job, f"{message}（返回数据 {job['succeeded']} 项，业务拒绝 {job['failed']} 项，无明细 {job['noDetails']} 项；已停止，不会自动重试）",
                          state="failed")
        finally:
            if acquired:
                acc.lock.release()
            self._release_active(job)


manager = GuildJobManager()
