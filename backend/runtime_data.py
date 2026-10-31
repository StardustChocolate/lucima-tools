"""Immutable game-data snapshots shared by all accounts and task threads."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from . import config

DATA_FILES = frozenset({
    "assets/abyss_battle_static.json", "assets/ark2_skill_static.json", "assets/game_config.json",
    "assets/localization/loc_CHS_FINAL.json", "assets/role_elements.json", "assets/role_jobs.json",
    "assets/item_categories.json", "backend/item_names.json", "backend/item_rank.json",
    "backend/equip_ref.json", "backend/activity_names.json", "backend/commodity_catalog.json",
    "assets/guild_store.json", "assets/guild_aid.json", "assets/guild_quests.json",
})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_pinned = ContextVar("game_data_snapshot", default=None)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_bytes(raw)
    temporary.replace(path)


class Snapshot:
    def __init__(self, version="bundled", files=None, manifest=None):
        self.version, self.files, self.manifest = version, files or {}, manifest or {}
        self.cache = {}
        self.lock = threading.RLock()

    def read(self, name, default=None):
        with self.lock:
            if name in self.files:
                return self.files[name]
            root = Path(os.environ.get("ARK_ASSET_ROOT") or Path(__file__).resolve().parents[1])
            path = Path(__file__).resolve().parent / Path(name).name if name.startswith("backend/") else root / name
            key = ("file", str(path))
            if key not in self.cache:
                try:
                    value = json.loads(path.read_text(encoding="utf-8-sig"))
                    if not isinstance(value, dict):
                        raise ValueError("游戏数据必须为对象")
                except (OSError, ValueError):
                    if default is None:
                        raise
                    value = default
                self.cache[key] = value
            return self.cache[key]


class Registry:
    def __init__(self, root=None):
        self._root = Path(root) if root else None
        self.lock = threading.RLock()
        self.active = Snapshot()
        self.previous = ""
        self.loaded = False

    @property
    def root(self):
        return self._root or config.data_dir() / "game-data"

    def restore(self, name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError("游戏数据版本无效")
        base = self.root / "versions" / name
        manifest = json.loads((base / "manifest.json").read_bytes())
        if (not isinstance(manifest, dict) or manifest.get("schema") != 1
                or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != DATA_FILES):
            raise ValueError("游戏数据清单不完整")
        files = {}
        for path, ref in manifest["files"].items():
            if not isinstance(ref, dict) or type(ref.get("size")) is not int or not isinstance(ref.get("sha256"), str):
                raise ValueError("游戏数据校验信息无效")
            raw = (base / path).read_bytes()
            if len(raw) != ref["size"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("游戏数据校验失败")
            files[path] = json.loads(raw)
            if not isinstance(files[path], dict):
                raise ValueError("游戏数据结构无效")
        return Snapshot(name, files, manifest)

    def current(self):
        with self.lock:
            if not self.loaded:
                self.loaded = True
                try:
                    saved = json.loads((self.root / "active.json").read_bytes())
                    if not isinstance(saved, dict):
                        saved = {}
                except (OSError, ValueError):
                    saved = {}
                for name in (saved.get("active"), saved.get("previous")):
                    try:
                        self.active = self.restore(name)
                        self.previous = saved.get("previous", "") if name == saved.get("active") else ""
                        break
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
            return self.active

    def publish(self, name, files, metadata, *, activate=True):
        if not NAME.fullmatch(name) or set(files) != DATA_FILES:
            raise ValueError("游戏数据集不完整")
        base = self.root / "versions" / name
        refs = {}
        for path, value in files.items():
            raw = encode(value)
            atomic(base / path, raw)
            refs[path] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        manifest = {**metadata, "schema": 1, "files": refs}
        atomic(base / "manifest.json", encode(manifest))
        snapshot = self.restore(name)
        return self.activate(snapshot) if activate else snapshot

    def activate(self, snapshot):
        name = snapshot.version
        with self.lock:
            old = self.current()
            previous = old.version if old.version != "bundled" and old.version != name else self.previous
            atomic(self.root / "active.json", encode({"schema": 1, "active": name, "previous": previous}))
            self.active, self.previous = snapshot, previous
        return snapshot


registry = Registry()


def snapshot():
    return _pinned.get() or registry.current()


def read_json(name, default=None):
    return snapshot().read(name, default)


@contextmanager
def pin():
    if _pinned.get() is not None:
        yield _pinned.get()
        return
    token = _pinned.set(registry.current())
    try:
        yield _pinned.get()
    finally:
        _pinned.reset(token)


def pinned(function):
    @wraps(function)
    def run(*args, **kwargs):
        with pin():
            return function(*args, **kwargs)
    return run


def cached(function):
    """Cache a no-argument loader inside its snapshot, not across updates."""
    key = ("derived", function.__module__, function.__qualname__)
    @wraps(function)
    def load():
        current = snapshot()
        with current.lock:
            if key not in current.cache:
                current.cache[key] = function()
            return current.cache[key]
    def clear():
        with snapshot().lock:
            snapshot().cache.clear()
    load.cache_clear = clear
    return load
