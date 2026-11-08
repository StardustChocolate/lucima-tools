"""本机 GVG 任务：短锁快照、账号隔离、请求幂等、停止与过期检查。"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid

from ..game_client import GameError
from .. import config
from . import adapters, search
from .catalog import catalog, versions, digest

START_LOCK = threading.Lock()


class Conflict(GameError): pass


class Job:
    def __init__(self, request_id, request, snapshot_hash):
        self.id = uuid.uuid4().hex; self.request_id = request_id; self.request = request
        self.input_hash = digest(request); self.snapshot_hash = snapshot_hash; self.versions = versions()
        self.lock = threading.Lock(); self.cancel = threading.Event(); self.started = time.monotonic()
        self.status = 'running'; self.progress = {}; self.result = None; self.error = None

    def update(self, value):
        with self.lock: self.progress = value

    def view(self):
        with self.lock:
            return {'jobId': self.id, 'requestId': self.request_id, 'status': self.status,
                    'elapsed': int(time.monotonic()-self.started), 'progress': copy.deepcopy(self.progress),
                    'snapshotHash': self.snapshot_hash, 'inputHash': self.input_hash, 'versions': self.versions,
                    **({'result': copy.deepcopy(self.result)} if self.status=='completed' else {}),
                    **({'error':self.error} if self.error else {})}


def _capture(acc):
    if not acc.lock.acquire(blocking=False): raise GameError('当前账号正在同步或执行任务，请稍后读取 GVG 资料')
    try:
        if not acc.client.account_state: raise GameError('请先登录账号')
        if getattr(acc, 'equipment_artifact_uncertain', False): raise GameError('羁绊状态尚未确认，请先在装备页同步账号')
        return adapters.snapshot(acc.client)
    finally: acc.lock.release()


def _current(acc, job_id, request_id=None):
    for job in getattr(acc, 'gvg_jobs', {}).values():
        if (job_id and job.id==job_id) or (request_id and job.request_id==request_id): return job
    raise GameError('GVG 配队任务已失效，请重新生成')


def _view(acc, job):
    value=job.view()
    if value['status']=='completed':
        if not acc.lock.acquire(blocking=False):
            value.pop('result',None);value['status']='verifying';return value
        try:
            changed = adapters.fingerprint(adapters.snapshot(acc.client)) != job.snapshot_hash or versions()!=job.versions
            changed |= bool(getattr(acc, 'equipment_artifact_uncertain', False))
        finally: acc.lock.release()
        if changed:
            with job.lock: job.status='expired'; job.result=None
            return job.view()
    return value


def handle(acc, action, body, avatar=lambda sid: None):
    if action=='overview':
        return adapters.roster(_capture(acc),avatar)
    if action in ('status','cancel','export'):
        job=_current(acc,body.get('jobId'),body.get('requestId'))
        if action=='cancel':
            with job.lock:
                if job.status=='running':job.cancel.set();job.status='cancelling'
        value=_view(acc,job)
        if action!='export':return value
        if value['status']!='completed':raise Conflict('方案已过期或尚未完成，请重新生成后导出')
        plan=next((p for p in value['result']['plans'] if p['id']==body.get('planId')),None)
        if not plan:raise GameError('未找到要导出的双队方案')
        payload={'format':'lucima-gvg-plan','schema':1,'createdAt':int(time.time()),
                 'request':job.request,'snapshotHash':job.snapshot_hash,'inputHash':job.input_hash,
                 'versions':job.versions,'plan':plan}
        filename='LucimaTools-GVG-'+re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', job.request['target'])[:60].strip(' .')+'.json'
        content=json.dumps(payload,ensure_ascii=False,indent=2)+'\n'
        if config.PLATFORM=='android':return {'filename':filename,'content':content}
        directory=config.get_download_directory();directory.mkdir(parents=True,exist_ok=True)
        path=directory/filename;index=1
        while path.exists():path=directory/f'{filename[:-5]}-{index}.json';index+=1
        path.write_text(content,encoding='utf-8')
        return {'ok':True,'path':str(path)}
    if action!='recommend':raise GameError('未知的 GVG 操作')
    request=adapters.normalize(body)
    request_id=body.get('requestId')
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',request_id):raise GameError('GVG 请求编号无效')
    with START_LOCK:
        jobs=getattr(acc,'gvg_jobs',{});existing=jobs.get(request_id)
        if existing:
            if existing.input_hash!=digest(request):raise Conflict('同一个请求编号的内容发生变化，请重新生成')
            return _view(acc,existing)
        if any(j.view()['status'] in ('running','cancelling') for j in jobs.values()):raise Conflict('已有 GVG 配队任务，请先停止或等待完成')
        state=_capture(acc)
        job=Job(request_id,request,adapters.fingerprint(state))
        jobs={k:j for k,j in list(jobs.items())[-11:]};jobs[request_id]=job;acc.gvg_jobs=jobs
    def run():
        try:
            data=adapters.roster(state,avatar)
            result=search.recommend(data,request,progress=job.update,cancelled=job.cancel.is_set)
            with job.lock:
                if not job.cancel.is_set():job.result=result;job.status='completed'
        except Exception as error:
            with job.lock:
                if not job.cancel.is_set():job.status='failed';job.error=str(error)
        finally:
            with job.lock:
                if job.cancel.is_set():job.status='cancelled';job.result=None
    threading.Thread(target=run,name='gvg-pair-search',daemon=True).start()
    return _view(acc,job)
