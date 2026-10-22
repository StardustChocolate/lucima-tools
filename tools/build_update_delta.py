"""Build and locally verify portable-file / byte-exact APK update deltas."""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import tempfile
import zipfile
from pathlib import Path

from backend.update_delta import (
    ANDROID_FORMAT, WINDOWS_FORMAT, apply_android, apply_windows, file_hash, windows_path,
)


def _write_recipe(archive: zipfile.ZipFile, value: dict) -> None:
    archive.writestr("recipe.json", json.dumps(value, separators=(",", ":")))


def _entry_hash(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> str:
    with archive.open(entry) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_windows(base: Path, target: Path, output: Path, version: str) -> dict:
    files = []
    with zipfile.ZipFile(base) as old, zipfile.ZipFile(target) as new, \
            zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as patch:
        previous = {x.filename: x for x in old.infolist() if not x.is_dir()}
        for entry in new.infolist():
            if entry.is_dir():
                continue
            name = windows_path(entry.filename)
            digest = _entry_hash(new, entry)
            prior = previous.get(name)
            changed = prior is None or prior.file_size != entry.file_size or _entry_hash(old, prior) != digest
            files.append({"path": name, "size": entry.file_size, "sha256": digest, "changed": changed})
            if changed:
                with new.open(entry) as src, patch.open("files/" + name, "w", force_zip64=True) as dst:
                    shutil.copyfileobj(src, dst)
        _write_recipe(patch, {"format": WINDOWS_FORMAT, "toVersion": version, "files": files})
    with tempfile.TemporaryDirectory(prefix="lucima-delta-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(base) as archive:
            for entry in archive.infolist():
                if not entry.is_dir():
                    windows_path(entry.filename)
            archive.extractall(root / "base")
        apply_windows(output, root / "base", root / "result", version)
        for item in files:
            assert file_hash(root / "result" / item["path"]) == item["sha256"]
    return {"format": WINDOWS_FORMAT}


def _payload_ranges(path: Path) -> list[tuple[int, int, str]]:
    """Locate raw compressed data using ZIP metadata; preserve all other bytes verbatim."""
    ranges = []
    with zipfile.ZipFile(path) as archive, path.open("rb") as stream:
        for entry in archive.infolist():
            if entry.is_dir() or not entry.compress_size:
                continue
            stream.seek(entry.header_offset)
            header = stream.read(30)
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise ValueError("Invalid APK local ZIP header")
            name_size, extra_size = struct.unpack_from("<HH", header, 26)
            offset = entry.header_offset + 30 + name_size + extra_size
            if offset + entry.compress_size > path.stat().st_size:
                raise ValueError("APK data outside archive")
            stream.seek(offset)
            digest = hashlib.sha256()
            remaining = entry.compress_size
            while remaining:
                chunk = stream.read(min(1024**2, remaining))
                if not chunk:
                    raise ValueError("Truncated APK")
                digest.update(chunk)
                remaining -= len(chunk)
            ranges.append((offset, entry.compress_size, digest.hexdigest()))
    return sorted(ranges)


def build_android(base: Path, target: Path, output: Path, version: str) -> dict:
    previous = {(size, digest): offset for offset, size, digest in _payload_ranges(base)}
    ops = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as patch:
        with target.open("rb") as src, patch.open("data", "w", force_zip64=True) as data:
            position = 0

            def insert(end: int) -> None:
                nonlocal position
                size = end - position
                if size <= 0:
                    return
                ops.append(["insert", size])
                src.seek(position)
                remaining = size
                while remaining:
                    chunk = src.read(min(1024**2, remaining))
                    if not chunk:
                        raise ValueError("Truncated target APK")
                    data.write(chunk)
                    remaining -= len(chunk)
                position = end

            for offset, size, digest in _payload_ranges(target):
                old_offset = previous.get((size, digest))
                if old_offset is None:
                    continue
                if offset < position:
                    raise ValueError("Overlapping APK entries")
                insert(offset)
                ops.append(["copy", old_offset, size])
                position = offset + size
            insert(target.stat().st_size)
        _write_recipe(patch, {"format": ANDROID_FORMAT, "toVersion": version, "operations": ops})
    descriptor = {"format": ANDROID_FORMAT, "baseSize": base.stat().st_size, "baseSha256": file_hash(base)}
    with tempfile.TemporaryDirectory(prefix="lucima-apk-delta-") as directory:
        apply_android(output, base, Path(directory) / "result.apk", {
            "size": target.stat().st_size, "sha256": file_hash(target),
        }, descriptor)
    return descriptor
