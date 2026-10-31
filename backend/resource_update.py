"""Check the official patch catalog and update runtime data independently of APKs."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
import re
import struct
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx

from . import config, runtime_data
from .maintenance import service as maintenance_service
from .logutil import log
from .runtime_build import BUILD_VERSION, compile_data
from .unity_text import BundleError, text_assets

MAINTENANCE_CHECK_INTERVAL = 5 * 60
CATALOG = re.compile(r"catalog_([0-9]+)\Z")
HASH = re.compile(r"[a-f0-9]{32}\Z")
SOURCE_PATTERNS = {
    "staticdata": re.compile(r"/staticdata_([a-f0-9]{32})\.bundle\Z"),
    "text": re.compile(r"/staticdata/text_([a-f0-9]{32})\.bundle\Z"),
    "localization_CHS": re.compile(r"/localization/chs\.asset_([a-f0-9]{32})\.bundle\Z"),
}


def catalog_sources(raw):
    catalog = json.loads(raw)
    ids = catalog["m_InternalIds"]
    selected = {}
    for index, name in enumerate(ids):
        for kind, pattern in SOURCE_PATTERNS.items():
            match = pattern.search(name)
            if match:
                if kind in selected:
                    raise ValueError("官方目录中数据包重复")
                selected[kind] = {"id": name, "hash": match[1], "index": index}
    if set(selected) != SOURCE_PATTERNS.keys():
        raise ValueError("官方目录缺少必需数据包")
    by_index = {row["index"]: row for row in selected.values()}
    entries = base64.b64decode(catalog["m_EntryDataString"], validate=True)
    extras = base64.b64decode(catalog["m_ExtraDataString"], validate=True)
    if len(entries) < 4 or len(entries) != 4 + struct.unpack_from("<i", entries)[0] * 28:
        raise ValueError("官方目录索引不完整")
    for entry in struct.iter_unpack("<7i", entries[4:]):
        if entry[0] not in by_index:
            continue
        pos = entry[4]
        if not 0 <= pos < len(extras) or extras[pos] != 7:
            raise ValueError("官方数据包描述无效")
        pos += 1
        for _ in range(2):
            pos += 1 + extras[pos]
        length = struct.unpack_from("<i", extras, pos)[0]; pos += 4
        if not 0 < length < 32768 or pos + length > len(extras):
            raise ValueError("官方数据包描述大小无效")
        properties = json.loads(extras[pos:pos + length].decode("utf-16-le"))
        size = properties.get("m_BundleSize")
        if type(size) is not int or not 0 < size <= 64 * 1024 * 1024:
            raise ValueError("官方数据包大小无效")
        by_index[entry[0]]["size"] = size
    if any("size" not in row for row in selected.values()):
        raise ValueError("官方数据包缺少大小信息")
    return selected


def localization(raw):
    result = {}
    for line in raw.decode("utf-8-sig").splitlines():
        if not line or line == "Key@Value":
            continue
        key, separator, value = line.partition("@")
        if not separator or not key or key in result:
            raise ValueError("官方简中文本格式无效")
        result[key] = value
    if not result:
        raise ValueError("官方简中文本为空")
    return result


def bulletin_info(value):
    if isinstance(value, dict) and isinstance(value.get("Info"), dict):
        value = value["Info"]
    if not isinstance(value, dict) or not CATALOG.fullmatch(str(value.get("NewCatalogName", ""))):
        raise ValueError("官方公告未返回有效资源版本")
    return value


class ResourceUpdates:
    def __init__(self, registry=None, transport=None, clock=time.time, builder=compile_data,
                 maintenance=maintenance_service):
        self.registry = registry or runtime_data.registry
        self.transport, self.clock, self.builder = transport, clock, builder
        self.maintenance = maintenance
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()
        self.wake = threading.Event()
        self.started = False
        self.state_loaded = False
        self.state = {}
        self.routine_at = 0
        self.maintenance.watch_poller(self)

    def _routine_deadline(self):
        # The software updater sets the shared deadline. Source-only testing
        # has no software updater thread, so use its same hourly policy there.
        with self.lock:
            if self.routine_at <= self.clock():
                from .updater import routine_check_delay
                self.routine_at = self.clock() + routine_check_delay()
            return self.routine_at

    def _next_check_at(self):
        return (self.clock() + MAINTENANCE_CHECK_INTERVAL if self.maintenance.active
                else self._routine_deadline())

    def maintenance_changed(self, active, epoch):
        with self.lock:
            self._load()
            if not self.state.get("checking"):
                # Reopening reported by a login also needs one final check
                # to activate data already staged during maintenance.
                self.state["nextCheckAt"] = (self.clock() + MAINTENANCE_CHECK_INTERVAL
                                             if active else self.clock())
                if active:
                    self.state.update(phase="waiting_server", error=None)
        self.wake.set()

    def on_software_check(self, next_delay):
        self.start()
        with self.lock:
            self._load()
            self.routine_at = self.clock() + next_delay
            if not self.maintenance.active and not self.state.get("checking"):
                # A startup/maintenance/manual check just completed can serve
                # this same tick; avoid querying the official server twice.
                self.state["nextCheckAt"] = (self.routine_at if
                    self.clock() - self.state.get("checkedAt", 0) < 60 else self.clock())
        self.wake.set()

    @property
    def root(self):
        return self.registry.root

    def _load(self):
        if not self.state_loaded:
            self.state_loaded = True
            try:
                saved = json.loads((self.root / "update-state.json").read_bytes())
                if isinstance(saved, dict):
                    self.state = saved
            except (OSError, ValueError):
                pass
            self.state.update(checking=False)

    def status(self):
        with self.lock:
            self._load()
            current = self.registry.current()
            return {**self.state, "version": current.manifest.get("catalog", ""),
                    "snapshot": current.version, "bundled": current.version == "bundled"}

    def _state(self, **values):
        with self.lock:
            self._load()
            self.state.update(values)
            atomic_state = {key: value for key, value in self.state.items() if key != "checking"}
            runtime_data.atomic(self.root / "update-state.json", runtime_data.encode(atomic_state))

    def _finish_check(self, **values):
        # Commit the deadline under the same lock as maintenance transitions;
        # a response arriving at check completion cannot lose the fast timer.
        with self.maintenance.lock:
            if values.get("phase") in {"ready", "waiting_server"}:
                values["phase"] = "waiting_server" if self.maintenance.active else "ready"
            self._state(checking=False, nextCheckAt=self._next_check_at(), **values)

    def _http(self, method, url, *, limit=1024 * 1024, target=None, payload=None):
        if self.transport:
            raw = self.transport(method, url, payload)
            if len(raw) > limit:
                raise ValueError("官方更新文件超出大小限制")
            if target:
                target.write_bytes(raw)
            return raw if not target else None
        with httpx.Client(proxy=config.get_proxy() or None, timeout=90, follow_redirects=True) as client:
            headers = {"Content-Type": "application/octet-stream"} if payload else {}
            content = runtime_data.encode(payload) if payload else None
            with client.stream(method, url, content=content, headers=headers) as response:
                response.raise_for_status()
                size = 0
                with target.open("wb") if target else tempfile.TemporaryFile() as output:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("官方更新文件超出大小限制")
                        output.write(chunk)
                    if target:
                        return None
                    output.seek(0)
                    return output.read()

    def _bulletin(self):
        token = self.maintenance.observation_token()
        raw = self._http("PUT", config.GAME_ROUTER, payload={"route": "GameServerDBSettingHandler.QueryBulletinInfoResult", "data": {}})
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        if self.maintenance.observe_response(raw.decode("utf-8", "replace")):
            from .game_client import MaintenanceError
            raise MaintenanceError("游戏维护中，5 分钟后重新检查")
        body = json.loads(raw)
        self.maintenance.observe(body, token=token)
        return bulletin_info(body)

    @staticmethod
    def _domain(bulletin):
        url = str(bulletin.get("PathDomain") or bulletin.get("PatchDomain") or
                  bulletin.get("PathDomains") or bulletin.get("PatchDomains") or "").rstrip("/") + "/"
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("官方补丁地址无效")
        return "https://" + parsed.netloc + parsed.path

    def _hash(self, base, catalog):
        value = self._http("GET", urljoin(base, "Android/" + catalog + ".hash"), limit=128).decode("ascii").strip().lower()
        if not HASH.fullmatch(value):
            raise ValueError("官方目录指纹无效")
        return value

    def _bundle(self, base, row):
        folder = self.root / "bundles"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (row["hash"] + ".bundle")
        proof = folder / (row["hash"] + ".json")
        if target.is_file() and proof.is_file():
            try:
                expected = json.loads(proof.read_bytes())
                if target.stat().st_size == row["size"] and hashlib.sha256(target.read_bytes()).hexdigest() == expected["sha256"]:
                    return target
            except (OSError, ValueError, KeyError, TypeError):
                pass
        identifier = row["id"]
        prefix = "http://PatchDomain/"
        if not identifier.startswith(prefix):
            raise ValueError("官方数据包路径无效")
        url = urljoin(base, identifier[len(prefix):])
        if not url.startswith(base + "Android/") or ".." in urlsplit(url).path.split("/"):
            raise ValueError("官方数据包路径越界")
        pending = target.with_suffix(".pending")
        try:
            self._http("GET", url, limit=row["size"], target=pending)
            if pending.stat().st_size != row["size"]:
                raise ValueError("官方数据包下载不完整")
            digest = hashlib.sha256(pending.read_bytes()).hexdigest()
            pending.replace(target)
            runtime_data.atomic(proof, runtime_data.encode({"sha256": digest, "size": row["size"]}))
        finally:
            pending.unlink(missing_ok=True)
        return target

    def _compile(self, bundles, catalog):
        with tempfile.TemporaryDirectory(prefix="build-", dir=self.root) as folder:
            source = Path(folder)
            tables = source / "文本/数据表"
            tables.mkdir(parents=True)
            for name, raw in text_assets(bundles["staticdata"]).items():
                if not re.fullmatch(r"[A-Za-z0-9_]+", name):
                    raise ValueError("官方数据表名称无效")
                (tables / ("sd_" + name + ".txt")).write_bytes(raw)
            loc = localization(text_assets(bundles["text"], names={"CHS"})["CHS"])
            supplement = text_assets(bundles["localization_CHS"], localization=True)
            for key, value in supplement.items():
                loc.setdefault(key, value)
            path = source / "文本/本地化/loc_CHS_FINAL.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(runtime_data.encode(loc))
            previous = self.registry.current()
            return self.builder(source, catalog.removeprefix("catalog_"), previous.read)

    def check(self, force=False):
        if not self.check_lock.acquire(blocking=False):
            return self.status()
        try:
            self._load()
            now = self.clock()
            if not force and now < self.state.get("nextCheckAt", 0):
                return self.status()
            self._state(checking=True, phase="checking", error=None)
            bulletin = self._bulletin()
            catalog = bulletin["NewCatalogName"]
            base = self._domain(bulletin)
            fingerprint = self._hash(base, catalog)
            active = self.registry.current()
            if (active.manifest.get("catalog") == catalog and active.manifest.get("catalogHash") == fingerprint
                    and active.manifest.get('buildVersion') == BUILD_VERSION):
                self._finish_check(phase="ready", checkedAt=now)
                return self.status()
            name = catalog + "-" + fingerprint[:12] + f'-b{BUILD_VERSION}'
            try:
                prepared = self.registry.restore(name)
                if prepared.manifest.get("catalogHash") != fingerprint or prepared.manifest.get('buildVersion') != BUILD_VERSION:
                    raise ValueError("资源目录指纹不符")
            except (OSError, ValueError, KeyError, TypeError):
                raw = self._http("GET", urljoin(base, "Android/" + catalog + ".json"), limit=40 * 1024 * 1024)
                sources = catalog_sources(raw)
                self._state(phase="downloading", availableVersion=catalog)
                bundles = {kind: self._bundle(base, row) for kind, row in sources.items()}
                self._state(phase="building")
                try:
                    files, report = self._compile(bundles, catalog)
                except (BundleError, UnicodeError):
                    # A complete-length transfer may still be corrupt. Do not
                    # keep retrying the same undecodable cached bytes forever.
                    for row in sources.values():
                        (self.root / "bundles" / (row["hash"] + ".json")).unlink(missing_ok=True)
                    raise
                prepared = self.registry.publish(name, files, {"catalog": catalog, "catalogHash": fingerprint,
                    "catalogSha256": hashlib.sha256(raw).hexdigest(), "sources": sources, "report": report,
                    "createdAt": now, 'buildVersion': BUILD_VERSION}, activate=False)
            latest = self._bulletin()
            if latest["NewCatalogName"] != catalog or self._hash(self._domain(latest), catalog) != fingerprint:
                raise ValueError("官方资源版本在下载期间变化，将重新检查")
            with self.maintenance.lock:
                if self.maintenance.active:
                    phase = "waiting_server"
                    log.info("[resources] %s 数据已准备，等待游戏维护结束", catalog)
                else:
                    self.registry.activate(prepared)
                    phase = "ready"
                    log.info("[resources] 已启用官方数据 %s：%s", catalog, prepared.manifest.get("report"))
                self._finish_check(phase=phase, checkedAt=self.clock(), availableVersion=catalog, error=None)
        except Exception as error:
            self._finish_check(phase="error", error=str(error), checkedAt=self.clock())
            log.warning("[resources] 游戏数据更新失败，继续使用原版本：%s", error)
        finally:
            self.check_lock.release()
        return self.status()

    def request_check(self):
        self.start(manual=True)
        with self.lock:
            self._load()
            self.state["nextCheckAt"] = 0
        self.wake.set()
        return {**self.status(), "checking": True}

    def observe(self, bulletin, token=None):
        self.maintenance.observe(bulletin, token=token)
        try:
            bulletin = bulletin_info(bulletin)
        except ValueError:
            return
        current = self.registry.current()
        if current.manifest.get("catalog") != bulletin["NewCatalogName"]:
            self.start()
            with self.lock:
                self._load()
                self.state["nextCheckAt"] = 0
            self.wake.set()

    def start(self, manual=False):
        with self.lock:
            if self.started:
                return
            self.started = True
            self._load()
            self.state["nextCheckAt"] = 0
        threading.Thread(target=self._run, name="game-data-update", daemon=True).start()

    def _run(self):
        while True:
            self.wake.clear()
            self.check()
            delay = max(1, self.status().get("nextCheckAt", self._next_check_at()) - self.clock())
            self.wake.wait(delay)


service = ResourceUpdates()
