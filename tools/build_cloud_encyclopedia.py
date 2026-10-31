"""Build portable cloud data from the production renderer bundle and media pool."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from stage_encyclopedia_cdn import VERSION, atomic_write

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def payload(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build(bundle, pool, avatars, version):
    if not VERSION.fullmatch(version):
        raise ValueError("Invalid cloud encyclopedia version")
    media = read(pool / "manifest.json")["files"]
    created = set()

    def put(raw, suffix):
        sha = hashlib.sha256(raw).hexdigest()
        relative = "objects/" + sha + suffix
        target = pool / relative
        if target.exists():
            if target.read_bytes() != raw:
                raise ValueError("Corrupt existing object: " + relative)
        else:
            atomic_write(target, raw)
            created.add(relative)
        return {"path": relative, "sha256": sha, "size": len(raw)}

    def media_ref(logical):
        props = media[logical]
        relative = f"objects/{props['sha256']}{Path(logical).suffix}"
        raw = (pool / relative).read_bytes()
        if len(raw) != props["size"] or hashlib.sha256(raw).hexdigest() != props["sha256"]:
            raise ValueError("Invalid media object: " + logical)
        return {"path": relative, **props}

    shared_files = {}
    def replace_assets(value):
        if isinstance(value, dict):
            return {key: replace_assets(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_assets(item) for item in value]
        if isinstance(value, str) and value.startswith("/assets/"):
            relative = value.removeprefix("/assets/")
            source = (bundle.parent / relative).resolve()
            if not source.is_relative_to(bundle.parent.resolve()):
                raise ValueError("Invalid shared asset: " + value)
            ref = put(source.read_bytes(), source.suffix)
            shared_files[ref["path"]] = ref
            return ref["path"]
        return value

    translations = replace_assets(read(bundle / "ui.json"))
    rows = []
    for entry in read(bundle / "index.json")["characters"]:
        hero = entry["id"]
        character = replace_assets(read(bundle / entry["detail"]))
        sprites = {skill["IconSprite"] for skill in character["Skill"] if skill.get("IconSprite")}
        logical = {"skills/" + sprite + ".png" for sprite in sprites}
        logical.update(path for path in media if path == f"portraits/static/{hero}.png" or path.startswith(f"portraits/spine/{hero}/"))
        refs = {path: media_ref(path) for path in sorted(logical)}
        detail = put(payload({"schema": 1, "character": character, "media": refs}), ".json")
        avatar = avatars / (hero + ".png")
        rows.append({**entry, "detail": detail,
                     "avatar": put(avatar.read_bytes(), ".png") if avatar.is_file() else None})
    index = put(payload({"schema": 1, "characters": rows}), ".json")
    ui = put(payload({"schema": 1, "translations": translations,
                      "files": sorted(shared_files.values(), key=lambda item: item["path"])}), ".json")
    latest = {"schema": 1, "kind": "lucima-encyclopedia", "renderer": 1,
              "version": version, "index": index, "ui": ui}
    raw = payload(latest)
    snapshot = pool / "versions" / (version + ".json")
    if snapshot.exists() and snapshot.read_bytes() != raw:
        raise ValueError("Cloud data version is immutable; choose a new version")
    atomic_write(snapshot, raw)
    atomic_write(pool / "latest.json", raw)
    report = {"version": version, "count": len(rows), "latestBytes": len(raw),
              "indexBytes": index["size"], "uiBytes": ui["size"],
              "createdObjects": sorted(created), "createdBytes": sum((pool / p).stat().st_size for p in created)}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "build/encyclopedia-bundle/encyclopedia")
    parser.add_argument("--pool", type=Path, default=ROOT / "releases/encyclopedia")
    parser.add_argument("--avatars", type=Path, default=ROOT / "assets/avatars")
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    report = build(args.bundle, args.pool, args.avatars, args.version)
    atomic_write(ROOT / "build/cloud-encyclopedia" / (args.version + ".json"), payload(report))
    print(json.dumps({key: value for key, value in report.items() if key != "createdObjects"}))


if __name__ == "__main__":
    main()
