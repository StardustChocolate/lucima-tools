"""Render complete encyclopedia PNGs and a portable query index, without cloud access."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from encyclopedia_search import build_search, normalize, phonetic

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://encyclopedia-cards.invalid"
RENDER_FILES = [
    "encyclopedia-card.css", "encyclopedia-scaling.js", "encyclopedia-palette.js",
    "encyclopedia-card.js", "encyclopedia-media.js", "encyclopedia-export.js",
    "encyclopedia-png-worker.js", "vendor/html-to-image.js", "vendor/UPNG.js",
    "vendor/pako_deflate.min.js",
]
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/encyclopedia-card.css"></head><body>
<script src="/static/encyclopedia-scaling.js"></script>
<script src="/static/encyclopedia-palette.js"></script>
<script src="/static/encyclopedia-card.js"></script>
<script src="/static/encyclopedia-media.js"></script>
<script src="/static/encyclopedia-export.js"></script></body></html>"""
EXPORT = """async ({entry, data, translations, config, version}) => {
    const mount = LucimaEncyclopediaMedia.mount;
    window.LucimaEncyclopediaMedia = {...LucimaEncyclopediaMedia, mount: options => {
        const media = mount(options);
        const required = [entry.hasStaticPortrait !== false,
            ...[...options.renderer.card.querySelectorAll('.character-skill__icon')]
                .map(icon => icon.dataset.iconSprite || '')
                .filter(sprite => /^Icon_Skill_[A-Za-z0-9_]+$/.test(sprite))
                .map(sprite => !Object.hasOwn(config.missingFiles || {}, `skills/${sprite}.png`))];
        media.ready = media.ready.then(results => {
            if (results.length !== required.length || results.some((ok, index) => !ok && required[index])) {
                throw new Error('Expected portrait or skill icon did not decode: ' + entry.id);
            }
            return results;
        });
        return media;
    }};
    try {
        return await LucimaEncyclopediaExport.render({entry, data, translations, config, version,
            fetchResource: async url => {
                const response = await fetch(url);
                if (!response.ok) throw new Error('Resource HTTP ' + response.status);
                return response;
            }});
    } finally { window.LucimaEncyclopediaMedia = {...LucimaEncyclopediaMedia, mount}; }
}"""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_bytes(data)
    os.replace(pending, path)


def verify_png(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or image.width != 1000 or image.height < 1200:
            raise ValueError("Unexpected card PNG dimensions")
        image.load()
        return image.width, image.height


def build(args):
    output = args.output.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.version):
        raise ValueError("Invalid version")
    bundle = args.bundle.resolve()
    entries = read_json(bundle / "index.json")["characters"]
    translations = read_json(bundle / "ui.json")
    details = {row["id"]: read_json(bundle / row["detail"]) for row in entries}
    source_root = args.resources.resolve()
    media_snapshot = source_root / "manifests" / f"{args.version}.json"
    config = read_json(media_snapshot if media_snapshot.is_file() else source_root / "manifest.json")
    config["baseUrl"] = ORIGIN + "/media/"
    inputs = {name: digest((ROOT / "frontend" / name).read_bytes()) for name in RENDER_FILES}
    inputs["exporter"] = digest(Path(__file__).read_bytes())
    inputs["ui"] = digest(json_bytes(translations))
    media = {}
    for logical, props in config["files"].items():
        if not logical.startswith(("portraits/static/", "skills/")):
            continue
        path = source_root / "objects" / (props["sha256"] + Path(logical).suffix)
        data = path.read_bytes()
        if len(data) != props["size"] or digest(data) != props["sha256"]:
            raise ValueError(f"Corrupt source media: {logical}")
        media["/media/objects/" + path.name] = path
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / (".build-" + args.version + ".json")
    state = read_json(state_path) if state_path.exists() else {
        "version": args.version, "generatedAt": datetime.now(timezone.utc).isoformat(), "cards": {}}
    if args.index_only:
        previous = read_json(output / (".report-" + args.version + ".json"))["renderEnvironment"]
        # Preserve the original renderer provenance while rebuilding query metadata.
        for key, value in inputs.items():
            if key != "exporter" and previous.get(key) != value:
                raise ValueError(f"Render input changed; rerender required: {key}")
        for entry in entries:
            hero = entry["id"]
            cached = state["cards"].get(hero)
            fingerprint = digest(json_bytes({"inputs": previous, "entry": entry, "data": details[hero],
                                            "resources": config["files"]}))
            if not cached or cached["fingerprint"] != fingerprint:
                raise ValueError(f"Card data changed; rerender required: {hero}")
            image = cached["image"]
            if image["path"] != f"objects/{image['sha256']}.png":
                raise ValueError(f"Unexpected cached image path: {hero}")
            data = (output / image["path"]).read_bytes()
            if len(data) != image["size"] or digest(data) != image["sha256"]:
                raise ValueError(f"Corrupt cached card: {hero}")
            if verify_png(data) != (image["width"], image["height"]):
                raise ValueError(f"Cached card dimensions changed: {hero}")
        write_catalog(args, entries, details, state, previous)
        return
    missing, errors = [], []

    def route_bundle(route):
        url = urlsplit(route.request.url)
        path = unquote(url.path)
        if url.netloc != urlsplit(ORIGIN).netloc:
            missing.append(route.request.url)
            route.abort()
            return
        if path == "/":
            route.fulfill(content_type="text/html", body=PAGE)
            return
        if path in media:
            file = media[path]
        elif path.startswith("/static/"):
            file = (ROOT / "frontend" / path.removeprefix("/static/")).resolve()
            if not file.is_relative_to(ROOT / "frontend"):
                raise ValueError("Invalid frontend resource path")
        elif path.startswith("/assets/"):
            file = (bundle.parent / path.removeprefix("/assets/")).resolve()
            if not file.is_relative_to(bundle.parent):
                raise ValueError("Invalid asset path")
        else:
            file = None
        if not file or not file.is_file():
            missing.append(path)
            route.fulfill(status=404, body="Missing export resource")
            return
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0]
                      or "application/octet-stream")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        inputs["browser"] = browser.version
        for font in ("msyh.ttc", "msyhbd.ttc", "msyhl.ttc"):
            path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font
            if path.is_file():
                inputs[font] = digest(path.read_bytes())
        context = browser.new_context(viewport={"width": 1440, "height": 1200},
                                      device_scale_factor=1, locale="zh-CN", reduced_motion="reduce")
        context.route("**/*", route_bundle)
        page = None
        rendered = 0
        for number, entry in enumerate(entries, 1):
            hero = entry["id"]
            if args.characters and hero not in args.characters:
                continue
            fingerprint = digest(json_bytes({"inputs": inputs, "entry": entry, "data": details[hero],
                                            "resources": config["files"]}))
            cached = state["cards"].get(hero)
            if cached and cached.get("fingerprint") == fingerprint:
                path = output / cached["image"]["path"]
                if path.is_file() and digest(path.read_bytes()) == cached["image"]["sha256"]:
                    print(f"[{number}/{len(entries)}] {hero} reused", flush=True)
                    continue
            if page is None or rendered % 20 == 0:
                if page is not None:
                    page.close()
                page = context.new_page()
                page.set_default_timeout(120000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(ORIGIN, wait_until="load")
            result = page.evaluate(EXPORT, {"entry": entry, "data": details[hero],
                                            "translations": translations, "config": config, "version": args.version})
            if missing or errors:
                raise ValueError(f"Incomplete render {hero}: {missing} {errors}")
            data = base64.b64decode(result["pngBase64"], validate=True)
            width, height = verify_png(data)
            sha256 = digest(data)
            path = f"objects/{sha256}.png"
            atomic_write(output / path, data)
            state["cards"][hero] = {"fingerprint": fingerprint, "image": {
                "path": path, "sha256": sha256, "size": len(data), "width": width, "height": height,
                "contentType": "image/png"}}
            atomic_write(state_path, json_bytes(state))
            rendered += 1
            print(f"[{number}/{len(entries)}] {hero} {width}x{height} {len(data)} bytes", flush=True)
        context.close()
        browser.close()
    if args.characters:
        print("Selected cards prepared; full index not published.", flush=True)
        return
    write_catalog(args, entries, details, state, inputs)


def write_catalog(args, entries, details, state, inputs):
    output = args.output.resolve()
    records = []
    for entry in entries:
        hero = entry["id"]
        detail = details[hero]
        image = state["cards"][hero]["image"]
        records.append({"id": hero, "name": entry["name"], "nameEn": entry.get("nameEn", ""),
                        "nameE7": detail.get("NAME_e7") or "", "aliases": entry.get("aliases", []),
                        "element": entry["element"], "job": entry["job"], "star": entry["star"],
                        "hasPortrait": bool(entry["hasStaticPortrait"]), "image": image})
    search = build_search(records)
    index = {"schema": 1, "kind": "lucima-encyclopedia-cards", "version": args.version,
             "generatedAt": state["generatedAt"], "language": "zh-CN", "count": len(records),
             "characters": records, "search": search}
    raw = json_bytes(index)
    index_path = output / "indexes" / (args.version + ".json")
    if index_path.exists() and index_path.read_bytes() != raw:
        raise ValueError("Published index version is immutable; use a new version")
    atomic_write(index_path, raw)
    manifest = {"schema": 1, "kind": index["kind"], "version": args.version,
                "generatedAt": state["generatedAt"], "baseUrl": "./", "count": len(records),
                "index": {"path": f"indexes/{args.version}.json", "sha256": digest(raw), "size": len(raw)}}
    atomic_write(output / "manifest.json", json_bytes(manifest))
    report = {"version": args.version, "cards": len(records),
              "totalImageBytes": sum(row["image"]["size"] for row in records),
              "missingPortraits": [row["id"] for row in records if not row["hasPortrait"]],
              "sourceGaps": read_json(args.resources / "manifest.json").get("missingFiles", {}),
              "queryCounts": {key: len(search[key]) for key in ("ids", "names", "aliases", "pinyin")},
              "ambiguousAliases": {key: value for key, value in search["aliases"].items() if len(value) > 1},
              "renderEnvironment": inputs}
    atomic_write(output / (".report-" + args.version + ".json"), json_bytes(report))
    print(json.dumps({k: v for k, v in report.items() if k != "renderEnvironment"}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "releases/encyclopedia/cards")
    parser.add_argument("--resources", type=Path, default=ROOT / "releases/encyclopedia")
    parser.add_argument("--bundle", type=Path, default=ROOT / "build/encyclopedia-bundle/encyclopedia")
    parser.add_argument("--index-only", action="store_true",
                        help="Rebuild query metadata after validating the existing cards and source data")
    parser.add_argument("--characters", nargs="+")
    args = parser.parse_args()
    if args.index_only and args.characters:
        parser.error("--index-only requires the full character collection")
    build(args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
