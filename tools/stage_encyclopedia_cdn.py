"""Prepare a shared, content-addressed encyclopedia media pool locally."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from stage_encyclopedia_resources import resource_sources


ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".skel", ".atlas", ".js"}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        pending.write_bytes(payload)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def stage_cdn(ark2_root: Path, output: Path, version: str) -> dict:
    if not VERSION.fullmatch(version):
        raise ValueError("Invalid resource version")
    sources, counts = resource_sources(ark2_root)
    files: dict[str, dict] = {}
    objects: dict[str, Path] = {}
    for logical, source in sorted(sources.items()):
        extension = Path(logical).suffix.lower()
        if extension not in EXTENSIONS or source.stat().st_size <= 0:
            raise ValueError(f"Invalid resource: {logical}")
        properties = {"sha256": file_hash(source), "size": source.stat().st_size}
        files[logical] = properties
        objects[f"objects/{properties['sha256']}{extension}"] = source
    manifest = {
        "schema": 2,
        "version": version,
        # Both the live entry and historical snapshots resolve to the same pool.
        "baseUrl": "/encyclopedia/",
        "staticPortraitBaseUrl": "portraits/static/",
        "dynamicBaseUrl": "portraits/spine/",
        "skillIconBaseUrl": "skills/",
        "spineRuntimeUrl": "runtime/spine-webgl.js",
        "files": files,
    }
    payload = json_bytes(manifest)
    snapshot = output / "manifests" / f"{version}.json"
    if snapshot.exists() and snapshot.read_bytes() != payload:
        raise FileExistsError(f"Resource version is immutable; choose a new version: {version}")
    entry = output / "manifest.json"
    previous = json.loads(entry.read_text(encoding="utf-8")) if entry.is_file() else {}
    previous_files = previous.get("files", {})
    created: list[str] = []
    added_bytes = 0
    for relative, source in objects.items():
        target = output / relative
        expected = Path(relative).stem
        if target.exists():
            if target.stat().st_size != source.stat().st_size or file_hash(target) != expected:
                raise ValueError(f"Existing content-addressed object is corrupt: {relative}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_name(f".{target.name}.{uuid.uuid4().hex}.pending")
        try:
            shutil.copy2(source, pending)
            if file_hash(pending) != expected:
                raise ValueError(f"Resource changed while staging: {source}")
            os.replace(pending, target)
        finally:
            pending.unlink(missing_ok=True)
        created.append(relative)
        added_bytes += target.stat().st_size
    # Publish the entry only after all referenced objects and its snapshot exist.
    if not snapshot.exists():
        atomic_write(snapshot, payload)
    atomic_write(entry, payload)
    categories = {}
    for prefix in ("portraits/static/", "portraits/spine/", "skills/", "runtime/"):
        group = [properties for logical, properties in files.items() if logical.startswith(prefix)]
        categories[prefix] = {"files": len(group), "bytes": sum(value["size"] for value in group)}
    return {
        "version": version,
        "previousVersion": previous.get("version"),
        "output": str(output.resolve()),
        "counts": counts,
        "logicalFiles": len(files),
        "logicalBytes": sum(properties["size"] for properties in files.values()),
        "objects": len(objects),
        "objectBytes": sum(source.stat().st_size for source in objects.values()),
        "manifestBytes": len(payload),
        "categories": categories,
        "changedFiles": [logical for logical, properties in files.items() if previous_files.get(logical) != properties],
        "newObjects": created,
        "newObjectBytes": added_bytes,
        "uploadLast": "manifest.json",
        "uploadManifests": [f"manifests/{version}.json", "manifest.json"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ark2-root", type=Path, default=Path(r"E:\Project\ark2"))
    parser.add_argument("--output", type=Path, default=ROOT / "releases" / "encyclopedia")
    parser.add_argument("--version", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = stage_cdn(args.ark2_root.resolve(), args.output.resolve(), args.version)
    report_path = args.report or ROOT / "build" / "encyclopedia-cdn" / f"{args.version}-upload.json"
    atomic_write(report_path, json_bytes(report))
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"changedFiles", "newObjects"}}, ensure_ascii=False, indent=2))
    print(f"Upload report: {report_path}")


if __name__ == "__main__":
    main()
