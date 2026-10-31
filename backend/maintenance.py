"""Process-wide maintenance gate for official bulletins and Server Maintain replies."""
from __future__ import annotations

import threading
import weakref
import json
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .logutil import log


_operation_epoch = ContextVar("maintenance_operation_epoch", default=None)


def is_maintenance_message(value):
    if isinstance(value, str):
        normalized = " ".join(value.split()).casefold()
        if normalized in {"server maintain", "server maintain_server maintain"}:
            return True
        if value.lstrip().startswith(('"', '{')):
            try:
                return is_maintenance_message(json.loads(value))
            except ValueError:
                pass
    elif isinstance(value, dict):
        return any(is_maintenance_message(value.get(key)) for key in
                   ("Error", "error", "Message", "message", "Msg", "msg", "Detail", "detail"))
    return False


class Maintenance:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = False
        self.epoch = 0
        self._server_time_ms = 0
        self._accounts = weakref.WeakSet()
        self._task_events = weakref.WeakSet()
        self._pollers = weakref.WeakSet()
        self._response_revision = 0

    def watch_poller(self, poller):
        with self.lock:
            self._pollers.add(poller)
            if self.active:
                poller.maintenance_changed(True, self.epoch)

    def observation_token(self):
        with self.lock:
            return self._response_revision

    def observe_response(self, value):
        if not is_maintenance_message(value):
            return False
        with self.lock:
            self._response_revision += 1
            self._transition(True)
        return True

    def track_event(self, event):
        with self.lock:
            self._task_events.add(event)
            if self.active:
                event.set()

    def watch(self, account):
        with self.lock:
            self._accounts.add(account)
            if self.active:
                account.maintenance_changed(True, self.epoch)

    def unwatch(self, account):
        with self.lock:
            self._accounts.discard(account)

    def observe(self, bulletin, token=None):
        if not isinstance(bulletin, dict):
            return
        info = bulletin.get("Info", bulletin)
        if not isinstance(info, dict):
            return
        value = info.get("IsOpen")
        # Missing, malformed and failed requests must never imply reopening.
        if type(value) not in (bool, int) or value not in (False, True):
            return
        active = not bool(value)
        stamp = bulletin.get("ServerTime")
        stamp = stamp.get("$date") if isinstance(stamp, dict) else None
        try:
            server_time_ms = int(stamp) if stamp is not None else int(float(bulletin.get("Utc", 0)) * 1000)
        except (TypeError, ValueError, OverflowError):
            server_time_ms = 0
        with self.lock:
            if not active and token is not None and token != self._response_revision:
                return  # A Server Maintain response superseded this in-flight bulletin.
            # Login and background checks can return out of order. An older
            # open bulletin must not reopen a more recent maintenance window.
            if server_time_ms and server_time_ms < self._server_time_ms:
                return
            self._server_time_ms = max(self._server_time_ms, server_time_ms)
            self._transition(active)

    def _transition(self, active):
        if self.active == active:
            return
        self.active = active
        if active:
            self.epoch += 1
            for event in list(self._task_events):
                event.set()
        log.info("[maintenance] %s epoch=%s", "维护开始，停止任务" if active else
                 "维护结束，唤醒账号重登", self.epoch)
        for observer in list(self._accounts) + list(self._pollers):
            try:
                observer.maintenance_changed(active, self.epoch)
            except Exception:
                log.exception("[maintenance] 通知维护状态失败")

    def require_open(self):
        with self.lock:
            from .game_client import MaintenanceError
            if self.active:
                raise MaintenanceError("游戏维护中，任务已停止，等待维护结束后自动重登")
            epoch = _operation_epoch.get()
            if epoch is not None and epoch != self.epoch:
                raise MaintenanceError("此操作已因服务器维护停止，请重新发起")

    @contextmanager
    def operation_scope(self):
        # Preserve the outer request's epoch when helpers nest scopes.
        token = _operation_epoch.set(self.epoch) if _operation_epoch.get() is None else None
        try:
            yield
        finally:
            if token is not None:
                _operation_epoch.reset(token)


service = Maintenance()


def operation(function):
    @wraps(function)
    def run(*args, **kwargs):
        with service.operation_scope():
            return function(*args, **kwargs)
    return run
