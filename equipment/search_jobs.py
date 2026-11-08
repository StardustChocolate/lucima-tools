"""本地完整配装任务：短请求查询进度，完整结束后才交付结果。"""
from __future__ import annotations

import copy
import threading
import time
import uuid

from backend.game_client import GameError

_START_LOCK = threading.Lock()


class SearchJob:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.started = time.monotonic()
        self.status = "running"
        self.progress = {}
        self.result = None
        self.error = None

    def update(self, value):
        with self.lock:
            self.progress = dict(value)

    def view(self):
        with self.lock:
            value = {"searchId":self.id,"status":self.status,"elapsed":int(time.monotonic()-self.started),
                     "progress":self.progress.copy()}
            if self.status == "done": value["result"] = copy.deepcopy(self.result)
            if self.error: value["error"] = self.error
            return value

    def stop(self):
        with self.lock:
            if self.status in ("running","cancelling"):
                self.cancel.set()
                self.status = "cancelling"


def current(acc, search_id):
    job = getattr(acc,"equipment_search_job",None)
    if job is None or search_id != job.id:
        raise GameError("配装计算任务已失效，请重新开始")
    return job


def start(acc, compute):
    with _START_LOCK:
        previous = getattr(acc,"equipment_search_job",None)
        if previous and previous.view()["status"] in ("running","cancelling"):
            raise GameError("已有配装计算正在进行，请等待完成或先停止计算")
        job = SearchJob()
        acc.equipment_search_job = job

    def run():
        try:
            if job.cancel.is_set(): return
            result = compute(job)
            with job.lock:
                if not job.cancel.is_set():
                    job.result = result
                    job.status = "done"
        except Exception as error:
            with job.lock:
                if not job.cancel.is_set():
                    job.status = "failed"
                    job.error = str(error)
        finally:
            with job.lock:
                if job.cancel.is_set():
                    job.status = "cancelled"
                    job.result = None

    threading.Thread(target=run,name="equipment-complete-search",daemon=True).start()
    return {"searchId":job.id,"status":"running"}
