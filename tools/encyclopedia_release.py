"""Validate a complete, portable encyclopedia publication without cloud access."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from stage_encyclopedia_cdn import VERSION, atomic_write, json_bytes


PLAN_NAME = ".release-plan.json"
HERO = re.compile(r"H\d{3}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
OBJECT = re.compile(r"objects/([a-f0-9]{64})\.(json|png|jpg|jpeg|webp|skel|atlas|js)\Z")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_file(pool, relative):
    path = PurePosixPath(relative)
    if (not relative or "\\" in relative or path.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("Invalid publication path: " + relative)
    result = (pool / relative).resolve()
    if not result.is_relative_to(pool.resolve()) or not result.is_file():
        raise ValueError("Publication file missing or outside output: " + relative)
    return result


@dataclass(frozen=True)
class File:
    path: str
    size: int
    sha256: str

    def descriptor(self):
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass
class Release:
    pool: Path
    version: str
    files: list[File]
    entries: list[File]
    versions: dict
    counts: dict

    def document(self):
        return {"schema": 1, "kind": "lucima-encyclopedia-release", "version": self.version,
                "versions": self.versions, "counts": self.counts,
                "files": [item.descriptor() for item in self.files],
                "entries": [item.descriptor() for item in self.entries]}


def collect_release(pool, *, version=None, cards=False):
    """Walk all references; accept no dependency on the old checked-in snapshots."""
    pool = pool.resolve()
    files = {}
    entries = []
    versions = {}
    counts = {}

    def checked(relative, descriptor=None):
        if descriptor is not None:
            if (type(descriptor.get("size")) is not int or descriptor["size"] <= 0
                    or not isinstance(descriptor.get("sha256"), str)
                    or not HASH.fullmatch(descriptor["sha256"])):
                raise ValueError("Invalid file descriptor: " + relative)
        cached = files.get(relative)
        if cached is None:
            path = local_file(pool, relative)
            cached = File(relative, path.stat().st_size, file_hash(path))
        if descriptor and (cached.size != descriptor["size"] or cached.sha256 != descriptor["sha256"]):
            raise ValueError("File checksum failed: " + relative)
        return cached

    def object_ref(ref, prefix="", *, json_only=False):
        relative = ref["path"]
        match = OBJECT.fullmatch(relative)
        if (not match or match[1] != ref["sha256"]
                or (json_only and match[2] != "json")):
            raise ValueError("Invalid object reference: " + relative)
        item = checked(prefix + relative, ref)
        files[item.path] = item
        return read(pool / item.path) if json_only else None

    def entry(relative, section, schema, kind=None):
        item = checked(relative)
        value = read(pool / relative)
        if value.get("schema") != schema or (kind and value.get("kind") != kind):
            raise ValueError("Unsupported entry: " + relative)
        token = value.get("version")
        if not isinstance(token, str) or not VERSION.fullmatch(token):
            raise ValueError("Invalid entry version: " + relative)
        versions[section] = token
        entries.append(item)
        return value, item

    def snapshot(relative, current):
        item = checked(relative, current.descriptor())
        files[relative] = item

    def characters(rows):
        if not isinstance(rows, list) or not rows:
            raise ValueError("Empty character collection")
        ids = [row.get("id") for row in rows]
        if any(not isinstance(hero, str) or not HERO.fullmatch(hero) for hero in ids) or len(set(ids)) != len(ids):
            raise ValueError("Invalid or duplicate character IDs")
        return set(ids)

    media, media_entry = entry("manifest.json", "media", 2)
    if media.get("baseUrl") != "/encyclopedia/" or not isinstance(media.get("files"), dict) or not media["files"]:
        raise ValueError("Invalid media manifest")
    for logical, props in media["files"].items():
        object_ref({"path": f"objects/{props['sha256']}{Path(logical).suffix.lower()}", **props})
    snapshot(f"manifests/{versions['media']}.json", media_entry)
    counts["mediaFiles"] = len(media["files"])

    data, data_entry = entry("latest.json", "data", 1, "lucima-encyclopedia")
    if data.get("renderer") != 1:
        raise ValueError("Unsupported renderer version")
    index = object_ref(data["index"], json_only=True)
    ui = object_ref(data["ui"], json_only=True)
    if index.get("schema") != 1 or ui.get("schema") != 1:
        raise ValueError("Unsupported cloud data schema")
    ids = characters(index["characters"])
    for ref in ui["files"]:
        object_ref(ref)
    for row in index["characters"]:
        detail = object_ref(row["detail"], json_only=True)
        if detail.get("schema") != 1 or detail["character"]["ID"] != row["id"]:
            raise ValueError("Character detail identity mismatch: " + row["id"])
        if row.get("avatar"):
            object_ref(row["avatar"])
        for ref in detail["media"].values():
            object_ref(ref)
    snapshot(f"versions/{versions['data']}.json", data_entry)
    counts["characters"] = len(ids)

    if cards:
        card, card_entry = entry("cards/manifest.json", "cards", 1, "lucima-encyclopedia-cards")
        if card.get("baseUrl") != "./" or card["index"]["path"] != f"indexes/{versions['cards']}.json":
            raise ValueError("Invalid card index path")
        item = checked("cards/" + card["index"]["path"], card["index"])
        files[item.path] = item
        catalog = read(pool / item.path)
        if (catalog.get("schema") != 1 or catalog.get("kind") != card["kind"]
                or catalog.get("version") != card["version"]
                or catalog.get("count") != len(catalog["characters"])
                or card.get("count") != len(ids) or characters(catalog["characters"]) != ids):
            raise ValueError("Card collection does not match the cloud catalog")
        for row in catalog["characters"]:
            if not row["image"]["path"].endswith(".png"):
                raise ValueError("Expected a PNG card")
            object_ref(row["image"], "cards/")
        counts["cards"] = len(ids)

    # Switch the app's data entry last, after legacy media and optional cards.
    entries.sort(key=lambda item: item.path == "latest.json")
    version = version or versions["data"]
    if not VERSION.fullmatch(version):
        raise ValueError("Invalid release version")
    return Release(pool, version, [files[key] for key in sorted(files)], entries, versions, counts)


def save_plan(release, destination=None):
    destination = destination or release.pool / PLAN_NAME
    atomic_write(destination, json_bytes(release.document()))


def load_plan(pool, path=None):
    path = path or pool / PLAN_NAME
    expected = read(path)
    release = collect_release(pool, version=expected["version"], cards="cards" in expected["versions"])
    if release.document() != expected:
        raise ValueError("Build output changed; regenerate the encyclopedia release plan")
    return release
