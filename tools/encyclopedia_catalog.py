"""Validate Ark2's character index against processed records and media."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


HERO_ID = re.compile(r"H\d{3}\Z")
INDEX_FIELDS = ("id", "number", "name", "element", "job", "hasStaticPortrait", "hasSpine")


def load_character_records(ark2_root: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    source_root = ark2_root / "out" / "assets" / "团员"
    data_root = source_root / "资料"
    index_path = data_root / "character_index.json"
    catalog = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or catalog.get("schema_version") != "1.0":
        raise ValueError(f"{index_path} 必须使用 schema_version 1.0")
    characters = catalog.get("characters")
    if not isinstance(characters, list) or not characters:
        raise ValueError(f"{index_path} 缺少非空 characters 数组")
    processed_ids = {path.stem for path in data_root.glob("H*.json") if HERO_ID.fullmatch(path.stem)}
    indexed_ids = [entry.get("id") for entry in characters if isinstance(entry, dict)]
    if len(indexed_ids) == len(characters) and all(isinstance(hero_id, str) for hero_id in indexed_ids):
        missing = sorted(processed_ids - set(indexed_ids))
        extra = sorted(set(indexed_ids) - processed_ids)
        if missing or extra:
            raise ValueError(f"{index_path} 与处理版 Hxxx.json 集合不一致；缺少 {missing}，多出 {extra}")

    records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()
    for entry in characters:
        if not isinstance(entry, dict) or any(field not in entry for field in INDEX_FIELDS):
            raise ValueError(f"{index_path} 有不完整的角色索引项：{entry!r}")
        hero_id = entry["id"]
        if not isinstance(hero_id, str) or not HERO_ID.fullmatch(hero_id) or hero_id in seen:
            raise ValueError(f"{index_path} 有无效或重复的角色 ID：{hero_id!r}")
        seen.add(hero_id)
        if type(entry["number"]) is not int or entry["number"] != int(hero_id[1:]):
            raise ValueError(f"{hero_id} 的角色编号与 ID 不匹配")
        if any(not isinstance(entry[field], str) or not entry[field] for field in ("name", "element", "job")):
            raise ValueError(f"{hero_id} 的名称、属性或职业无效")
        if any(type(entry[field]) is not bool for field in ("hasStaticPortrait", "hasSpine")):
            raise ValueError(f"{hero_id} 的资源标志必须是布尔值")

        source_path = data_root / f"{hero_id}.json"
        record = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("schema_version") != "2.0-processed":
            raise ValueError(f"{source_path} 必须是 2.0-processed 处理版资料")
        for index_field, record_field in (("id", "ID"), ("name", "NAME"), ("element", "Element"), ("job", "Job")):
            if entry[index_field] != record.get(record_field):
                raise ValueError(f"{hero_id} 的索引 {index_field} 与处理版资料 {record_field} 不一致")

        portrait = source_root / "立绘" / f"{hero_id}.png"
        spine_root = source_root / "动态立绘" / hero_id
        skeleton = spine_root / f"{hero_id}.skel"
        atlas = spine_root / f"{hero_id}.atlas"
        if skeleton.is_file() != atlas.is_file():
            raise ValueError(f"{hero_id} 的规范化 Spine 骨骼和 atlas 不成对")
        if entry["hasStaticPortrait"] != portrait.is_file():
            raise ValueError(f"{hero_id} 的 hasStaticPortrait 与规范化立绘文件不一致")
        if entry["hasSpine"] != (skeleton.is_file() and atlas.is_file()):
            raise ValueError(f"{hero_id} 的 hasSpine 与规范化 Spine 文件不一致")
        records.append((entry, record))

    return records
