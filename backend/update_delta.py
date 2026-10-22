"""Bounded, streaming reconstruction of signed release deltas (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

WINDOWS_FORMAT = "windows-file-delta-v1"
ANDROID_FORMAT = "android-archive-delta-v1"
MAX_OUTPUT = 2 * 1024**3
MAX_FILES = 50000
MAX_RECIPE = 16 * 1024**2
_HASH = re.compile(r"^[0-9a-f]{64}$")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class DeltaError(ValueError):
    pass


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def windows_path(name: str) -> str:
    parts = name.split("/")
    if (not name or len(name) > 1000 or any(
        not part or part in {".", ".."} or part.endswith((".", " "))
        or any(ord(c) < 32 or c in '\\:<>"|?*' for c in part)
        or _RESERVED.match(part) for part in parts
    ) or not (name == "LucimaTools.exe" or name.startswith("_internal/"))):
        raise DeltaError("Invalid Windows delta path")
    return name


def _integer(value: object, maximum: int = MAX_OUTPUT) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise DeltaError("Invalid delta size or offset")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise DeltaError("Invalid delta hash")
    return value


def _recipe(archive: zipfile.ZipFile, kind: str) -> dict:
    entries = archive.infolist()
    if len(entries) > MAX_FILES + 1 or len({x.filename for x in entries}) != len(entries):
        raise DeltaError("Invalid delta archive entries")
    entry = archive.getinfo("recipe.json")
    if entry.file_size > MAX_RECIPE:
        raise DeltaError("Delta recipe too large")
    data = json.loads(archive.read(entry))
    if not isinstance(data, dict) or data.get("format") != kind:
        raise DeltaError("Unsupported delta recipe")
    return data


def _copy(source, target, size: int, digest=None) -> None:
    remaining = size
    while remaining:
        chunk = source.read(min(1024**2, remaining))
        if not chunk:
            raise DeltaError("Truncated delta input")
        target.write(chunk)
        if digest is not None:
            digest.update(chunk)
        remaining -= len(chunk)


def apply_windows(patch: Path, base: Path, stage: Path, version: str) -> None:
    """Create a complete target tree; the caller owns cleanup on failure."""
    base = base.resolve()
    stage.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(patch) as archive:
        recipe = _recipe(archive, WINDOWS_FORMAT)
        if recipe.get("toVersion") != version:
            raise DeltaError("Delta version mismatch")
        files = recipe.get("files")
        if not isinstance(files, list) or not 0 < len(files) <= MAX_FILES:
            raise DeltaError("Invalid delta file list")
        seen = set()
        total = 0
        for item in files:
            name = windows_path(item["path"])
            if name.casefold() in seen:
                raise DeltaError("Duplicate Windows delta path")
            seen.add(name.casefold())
            size = _integer(item["size"])
            total += size
            if total > MAX_OUTPUT:
                raise DeltaError("Delta output too large")
            expected = _digest(item["sha256"])
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            if item.get("changed") is True:
                entry = archive.getinfo("files/" + name)
                if entry.file_size != size:
                    raise DeltaError("Delta entry size mismatch")
                with archive.open(entry) as src, target.open("wb") as dst:
                    _copy(src, dst, size, digest)
            elif item.get("changed") is False:
                source = (base / name).resolve()
                if not source.is_relative_to(base) or source.stat().st_size != size:
                    raise DeltaError("Installed base file mismatch")
                with source.open("rb") as src, target.open("wb") as dst:
                    _copy(src, dst, size, digest)
            else:
                raise DeltaError("Invalid delta file source")
            if digest.hexdigest() != expected:
                raise DeltaError("Reconstructed file hash mismatch")
        if not (stage / "LucimaTools.exe").is_file() or not (stage / "_internal").is_dir():
            raise DeltaError("Invalid reconstructed Windows bundle")


def apply_android(patch: Path, base: Path, target: Path, artifact: dict, delta: dict) -> None:
    """Restore the exact signed APK, including ZIP metadata and signing blocks."""
    if base.stat().st_size != delta["baseSize"] or file_hash(base) != delta["baseSha256"]:
        raise DeltaError("Installed APK does not match delta baseline")
    with zipfile.ZipFile(patch) as archive:
        recipe = _recipe(archive, ANDROID_FORMAT)
        ops = recipe.get("operations")
        if not isinstance(ops, list) or not 0 < len(ops) <= 100000:
            raise DeltaError("Invalid APK delta operations")
        data_entry = archive.getinfo("data")
        if data_entry.file_size > MAX_OUTPUT:
            raise DeltaError("APK delta data too large")
        total = 0
        inserted = 0
        digest = hashlib.sha256()
        with base.open("rb") as old, archive.open(data_entry) as data, target.open("xb") as out:
            for op in ops:
                if not isinstance(op, list) or len(op) not in (2, 3):
                    raise DeltaError("Invalid APK delta operation")
                size = _integer(op[-1])
                total += size
                if total > artifact["size"] or total > MAX_OUTPUT:
                    raise DeltaError("APK delta output too large")
                if op[0] == "copy" and len(op) == 3:
                    offset = _integer(op[1])
                    if offset + size > delta["baseSize"]:
                        raise DeltaError("APK copy range outside baseline")
                    old.seek(offset)
                    _copy(old, out, size, digest)
                elif op[0] == "insert" and len(op) == 2:
                    inserted += size
                    _copy(data, out, size, digest)
                else:
                    raise DeltaError("Unknown APK delta operation")
        if inserted != data_entry.file_size or total != artifact["size"] \
                or digest.hexdigest() != artifact["sha256"]:
            raise DeltaError("Reconstructed APK verification failed")
