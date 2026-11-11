"""Versioned cloud encyclopedia; verified objects persist outside the install tree."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

from . import config
from .encyclopedia_resources import resource_config

OBJECT = re.compile(r"objects/([a-f0-9]{64})\.(json|png|jpg|jpeg|webp|skel|atlas)\Z")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
CHECK_INTERVAL = 3600
MAX_OBJECT = 32 * 1024 * 1024
API_ROOT = "/api/encyclopedia/"


class EncyclopediaError(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_bytes(raw)
    pending.replace(path)


def reference(value):
    if not isinstance(value, dict):
        raise EncyclopediaError("图鉴资源引用无效")
    match = OBJECT.fullmatch(str(value.get("path", "")))
    size = value.get("size")
    if not match or match[1] != value.get("sha256") or type(size) is not int or not 0 < size <= MAX_OBJECT:
        raise EncyclopediaError("图鉴资源引用无效")
    return value


def local_urls(value):
    if isinstance(value, dict):
        return {key: local_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [local_urls(item) for item in value]
    return API_ROOT + value if isinstance(value, str) and OBJECT.fullmatch(value) else value


class Encyclopedia:
    def __init__(self, root=None, endpoint=None, client=None, clock=time.time):
        self.root = Path(root) if root else config.data_dir() / "encyclopedia-cache"
        self.endpoint = endpoint
        self.client = client
        self.clock = clock
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()
        self.object_lock = threading.Lock()
        self.object_locks = {}
        self.objects = {}
        self.snapshots = {}
        self.active = None
        self.state = {}
        self.loaded = False

    def _location(self):
        endpoint = self.endpoint or resource_config().get("manifestUrl", "")
        if not endpoint:
            raise EncyclopediaError("未配置图鉴资料地址")
        return endpoint if endpoint.endswith("latest.json") else urljoin(endpoint, "latest.json")

    def _get(self, url, headers=None, limit=MAX_OBJECT):
        if self.client:
            return self.client(url, headers or {}, limit)
        with httpx.Client(proxy=config.get_proxy() or None, timeout=15, follow_redirects=True) as client:
            with client.stream("GET", url, headers=headers or {}) as response:
                if response.status_code == 304:
                    return 304, dict(response.headers), b""
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise EncyclopediaError("图鉴资源大小超出限制")
                    chunks.append(chunk)
                return response.status_code, dict(response.headers), b"".join(chunks)

    def _object(self, ref):
        reference(ref)
        name = ref["path"]
        with self.object_lock:
            guard = self.object_locks.setdefault(name, threading.Lock())
        with guard:
            target = self.root / name
            if target.is_file():
                raw = target.read_bytes()
                if len(raw) == ref["size"] and hashlib.sha256(raw).hexdigest() == ref["sha256"]:
                    return raw
            _, _, raw = self._get(urljoin(self._location(), name), limit=ref["size"])
            if len(raw) != ref["size"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise EncyclopediaError("图鉴资源校验失败")
            atomic(target, raw)
            return raw

    def _register(self, ref):
        reference(ref)
        with self.lock:
            existing = self.objects.get(ref["path"])
            if existing and existing != ref:
                raise EncyclopediaError("图鉴资源引用冲突")
            self.objects[ref["path"]] = ref

    def _snapshot(self, entry):
        if not isinstance(entry, dict) or entry.get("schema") != 1 or entry.get("renderer") not in (1, 2):
            raise EncyclopediaError("该图鉴资料需要更新软件后查看")
        if entry.get("kind") != "lucima-encyclopedia" or not VERSION.fullmatch(str(entry.get("version", ""))):
            raise EncyclopediaError("图鉴版本入口无效")
        reference(entry.get("index")); reference(entry.get("ui"))
        index = json.loads(self._object(entry["index"]))
        shared = json.loads(self._object(entry["ui"]))
        if not isinstance(index, dict) or not isinstance(shared, dict):
            raise EncyclopediaError("图鉴公共资料格式无效")
        rows = index.get("characters")
        if index.get("schema") != 1 or not isinstance(rows, list) or not rows or len(rows) > 5000:
            raise EncyclopediaError("图鉴角色索引无效")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise EncyclopediaError("图鉴角色索引无效")
            hero = row.get("id")
            if not re.fullmatch(r"H[0-9]{3}", str(hero)) or hero in seen or not row.get("name"):
                raise EncyclopediaError("图鉴角色索引无效")
            seen.add(hero)
            reference(row.get("detail"))
            if row.get("avatar"):
                reference(row["avatar"])
        search = index.get("search")
        if search is not None:
            tables = {"ids", "names", "aliases", "pinyin"}
            if not isinstance(search, dict) or set(search) != tables:
                raise EncyclopediaError("图鉴搜索索引无效")
            for table in search.values():
                if not isinstance(table, dict) or len(table) > 100000:
                    raise EncyclopediaError("图鉴搜索索引无效")
                for term, candidates in table.items():
                    if (not isinstance(term, str) or not term or not isinstance(candidates, list)
                            or not candidates or len(candidates) > len(seen)
                            or any(not isinstance(hero, str) or hero not in seen for hero in candidates)
                            or len(candidates) != len(set(candidates))):
                        raise EncyclopediaError("图鉴搜索索引无效")
        if shared.get("schema") != 1 or not isinstance(shared.get("translations"), dict) or not isinstance(shared.get("files"), list):
            raise EncyclopediaError("图鉴公共资料无效")
        refs = [ref for row in rows for ref in [row.get("avatar")] if ref] + shared["files"]
        for ref in refs:
            reference(ref)
        # Register only after the entire snapshot has passed validation.
        for ref in refs:
            self._register(ref)
        snapshot = {"entry": entry, "index": index, "ui": shared["translations"]}
        with self.lock:
            self.snapshots[entry["version"]] = snapshot
        return snapshot

    def _load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            self.state = json.loads((self.root / "state.json").read_bytes())
            if not isinstance(self.state, dict):
                self.state = {}
        except (OSError, ValueError):
            self.state = {}
        try:
            entry = json.loads((self.root / "active.json").read_bytes())
            # Loading a saved snapshot must never trigger network requests.
            for ref in (entry["index"], entry["ui"]):
                reference(ref)
                raw = (self.root / ref["path"]).read_bytes()
                if len(raw) != ref["size"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                    raise EncyclopediaError("本地图鉴缓存无效")
            self._snapshot(entry)
            self.active = entry["version"]
        except (OSError, ValueError, KeyError, TypeError, EncyclopediaError):
            self.active = None
            if not self.state.get("failures"):
                self.state["nextCheck"] = 0

    def catalog(self):
        with self.lock:
            self._load()
            status = {"error": self.state.get("error", ""),
                      "checkAfter": max(1, self.state.get("nextCheck", 0) - self.clock())}
            if not self.active:
                return {"available": False, "version": "", **status}
            snap = self.snapshots[self.active]
            rows = [{**row, "avatar": API_ROOT + row["avatar"]["path"] if row.get("avatar") else ""}
                    for row in snap["index"]["characters"]]
            return {"available": True, "version": self.active, "characters": rows,
                    "search": snap["index"].get("search"),
                    "ui": local_urls(snap["ui"]), **status}

    def check(self, force=False):
        with self.check_lock:
            with self.lock:
                self._load()
            now = self.clock()
            if now < self.state.get("nextCheck", 0) and (not force or now - self.state.get("attemptedAt", 0) < 5):
                return {"updated": False, **self.catalog()}
            self.state["attemptedAt"] = now
            headers = {"Cache-Control": "no-cache"}
            if self.active and self.state.get("etag"):
                headers["If-None-Match"] = self.state["etag"]
            before = self.active
            try:
                status, response_headers, raw = self._get(self._location(), headers, limit=16384)
                if status != 304:
                    entry = json.loads(raw)
                    if not isinstance(entry, dict):
                        raise EncyclopediaError("图鉴版本入口无效")
                    version = str(entry.get("version", ""))
                    if not VERSION.fullmatch(version):
                        raise EncyclopediaError("图鉴版本入口无效")
                    history = self.root / "versions" / (version + ".json")
                    if history.exists() and json.loads(history.read_bytes()) != entry:
                        raise EncyclopediaError("图鉴版本内容发生冲突")
                    if entry.get("version") == self.active and entry != self.snapshots[self.active]["entry"]:
                        raise EncyclopediaError("图鉴版本内容发生冲突")
                    self._snapshot(entry)
                    atomic(self.root / "versions" / (entry["version"] + ".json"), encoded(entry))
                    atomic(self.root / "active.json", encoded(entry))
                    with self.lock:
                        self.active = entry["version"]
                    self.state["etag"] = response_headers.get("etag", "")
                elif not self.active:
                    raise EncyclopediaError("图鉴缓存不可用，请重试")
                self.state.update(checkedAt=now, nextCheck=now + CHECK_INTERVAL, failures=0, error="")
            except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, EncyclopediaError) as error:
                failures = self.state.get("failures", 0) + 1
                message = str(error) if isinstance(error, EncyclopediaError) else "暂时无法获取图鉴资料，请检查网络后重试"
                self.state.update(failures=failures, nextCheck=now + min(120 * 2 ** min(failures - 1, 4), 1800), error=message)
            atomic(self.root / "state.json", encoded(self.state))
            return {"updated": bool(self.active and before != self.active), **self.catalog()}

    def detail(self, version, hero):
        if not VERSION.fullmatch(str(version)) or not re.fullmatch(r"H[0-9]{3}", str(hero)):
            raise EncyclopediaError("图鉴角色参数无效")
        with self.lock:
            self._load()
            if version not in self.snapshots:
                try:
                    entry = json.loads((self.root / "versions" / (version + ".json")).read_bytes())
                    self._snapshot(entry)
                except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
                    raise EncyclopediaError("该图鉴版本尚未缓存，请返回列表刷新") from None
            snap = self.snapshots[version]
            row = next((row for row in snap["index"]["characters"] if row["id"] == hero), None)
            if not row:
                raise EncyclopediaError("该角色不在图鉴中")
        try:
            detail = json.loads(self._object(row["detail"]))
            if not isinstance(detail, dict) or detail.get("schema") != 1 or not isinstance(detail.get("character"), dict) or detail["character"].get("ID") != hero or not isinstance(detail.get("media"), dict):
                raise EncyclopediaError("图鉴角色资料无效")
            with self.lock:
                for ref in detail["media"].values():
                    self._register(ref)
            return {"version": version, "character": local_urls(detail["character"]),
                    "media": detail["media"]}
        except (OSError, ValueError, httpx.HTTPError):
            raise EncyclopediaError("该角色资料尚未缓存，联网后重试") from None

    def object(self, name):
        with self.lock:
            self._load()
            ref = self.objects.get("objects/" + name)
        if not ref:
            raise EncyclopediaError("图鉴资源未列入当前资料")
        try:
            return self._object(ref), mimetypes.guess_type(name)[0] or "application/octet-stream"
        except (OSError, httpx.HTTPError):
            raise EncyclopediaError("图鉴图片尚未缓存，联网后重试") from None


service = Encyclopedia()
