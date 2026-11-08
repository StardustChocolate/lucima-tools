"""Rebuild runtime game indexes from one verified full resource snapshot.

Only metadata is written. Character AI uses Ark2's processed dossiers rather
than a second DSL parser; source table hashes and the formal roster are checked
before any output changes. Existing historical equipment IDs and extra fields
are retained. Use --output-root to review an isolated build first.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from .build_battle_metadata import BATTLE_PATH, build_battle_metadata
except ImportError:
    from build_battle_metadata import BATTLE_PATH, build_battle_metadata


ROOT = Path(__file__).resolve().parents[1]
STATIC_PATH = Path("assets/ark2_skill_static.json")
AI_PATH = Path("assets/auto_cast_rules.json")
EQUIPMENT_PATH = Path("backend/equip_ref.json")
GAME_CONFIG_PATH = Path("assets/game_config.json")
HERO_ID = re.compile(r"H\d{3}\Z")
SKILL_FIELDS = (
    "ID", "TargetTypeFun", "SkillType", "TargetType", "TargetCamp", "NoSelf",
    "CoolDown", "Soul", "SkillSoulCost", "SP", "IsInitCoolDown", "TwinSkill",
    "ReferanceSkillID", "LevelSkillID",
)
ROLE_FIELDS = ("SkillIDs", "NormalSkillID", "AIs", "HP", "Attack", "Defence", "Speed", "Element", "Job")
HERO_FIELDS = ("NormalSkillID", "SkillIDs", "AIs", "Element", "Job")
SLOTS = {"Weapon": "weapon", "Head": "helmet", "Body": "armor", "Necklace": "necklace", "Ring": "ring", "Shoes": "boots"}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_object(path: Path, *, optional: bool = False) -> dict[str, Any]:
    if optional and not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON must contain an object: {path}")
    return data


def load_table(source: Path, name: str, required: tuple[str, ...]) -> dict[str, dict[str, str]]:
    path = source / "文本" / "数据表" / f"sd_{name}.txt"
    lines = path.read_bytes().decode("utf-8-sig", "replace").splitlines()
    if not lines:
        raise ValueError(f"Empty source table: {path}")
    headers = lines[0].split("@")
    if len(headers) != len(set(headers)) or not set(required).issubset(headers):
        raise ValueError(f"Missing or duplicate source columns in {path}")
    rows: dict[str, dict[str, str]] = {}
    for line_number, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        values = line.split("@")
        if len(values) == len(headers) + 1 and not values[-1]:
            values.pop()
        if len(values) != len(headers):
            raise ValueError(f"Malformed source row in {path}:{line_number}")
        row = dict(zip(headers, values))
        identifier = row["ID"]
        if not identifier or identifier != identifier.strip() or identifier in rows:
            raise ValueError(f"Empty, invalid or duplicate ID in {path}:{line_number}: {identifier!r}")
        if any("\ufffd" in row[field] for field in required):
            raise ValueError(f"Invalid encoding in a consumed field in {path}:{line_number}")
        rows[identifier] = row
    if not rows:
        raise ValueError(f"No records in source table: {path}")
    return rows


def preserve_extras(old: Any, generated: dict[str, Any]) -> dict[str, Any]:
    if old is not None and not isinstance(old, dict):
        raise ValueError("Previous metadata record must be an object")
    return {**copy.deepcopy(old or {}), **generated}


def source_provenance(source: Path, raw_manifest: dict[str, Any], version: str | None) -> dict[str, Any]:
    tables = raw_manifest.get("tables")
    if raw_manifest.get("schema_version") != "2.0-raw" or not isinstance(tables, dict):
        raise ValueError("Character raw manifest must use schema_version 2.0-raw")
    for name in ("Role", "Skill", "SkillLevel", "RoleAwaken", "AIAction"):
        if name not in tables:
            raise ValueError(f"Raw character manifest omits {name}")
    hashes: dict[str, str] = {}
    for name, entry in sorted(tables.items()):
        if not re.fullmatch(r"[A-Za-z0-9_]+", name) or not isinstance(entry, dict):
            raise ValueError("Invalid table entry in raw character manifest")
        path = source / "文本" / "数据表" / f"sd_{name}.txt"
        actual = sha256(path)
        if entry.get("sha256") != actual:
            raise ValueError(f"Stale character JSON: raw manifest does not match sd_{name}.txt")
        hashes[name] = actual
    for name in ("Equipment", "Item"):
        hashes[name] = sha256(source / "文本" / "数据表" / f"sd_{name}.txt")
    localization_hash = sha256(source / "文本" / "本地化" / "loc_CHS_FINAL.json")
    evidence = source / "文本" / "文本提取报告.json"
    extraction = read_object(evidence, optional=True)
    if extraction:
        if extraction.get("verification_errors"):
            raise ValueError("Text extraction report contains verification errors")
        details = extraction.get("static_table_details")
        if not isinstance(details, list):
            raise ValueError("Text extraction report omits static_table_details")
        indexed = {entry.get("name"): entry for entry in details if isinstance(entry, dict)}
        for name, actual in hashes.items():
            if indexed.get(name, {}).get("sha256") != actual:
                raise ValueError(f"Text extraction report does not match sd_{name}.txt")
        if extraction.get("chs_merge", {}).get("final_sha256") != localization_hash:
            raise ValueError("Text extraction report does not match loc_CHS_FINAL.json")
    catalog = str(extraction.get("catalog") or "").replace("\\", "/").rsplit("/", 1)[-1]
    catalog_version = re.fullmatch(r"catalog_([A-Za-z0-9_.-]+)\.json", catalog)
    source_fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("ascii")).hexdigest()
    label = version or (catalog_version.group(1) if catalog_version else f"tables-{source_fingerprint[:12]}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
        raise ValueError("Source version must be a simple version label")
    return {
        "version": f"ark2-{label}", "catalog": catalog,
        "catalog_build_hash": extraction.get("catalog_build_hash") or "",
        "staticdata_content_hash": extraction.get("staticdata_bundle", {}).get("content_hash") or "",
        "table_sha256": hashes, "localization_sha256": localization_hash,
    }


def build_static(skills: dict, roles: dict, old: dict, provenance: dict) -> dict:
    hero_ids = {identifier for identifier, role in roles.items() if role["IsHero"] == "1"}
    if not hero_ids or any(not HERO_ID.fullmatch(identifier) for identifier in hero_ids):
        raise ValueError("The source formal character roster is empty or invalid")
    result_skills = {
        identifier: preserve_extras(old.get("skills", {}).get(identifier), {field: row[field] for field in SKILL_FIELDS})
        for identifier, row in sorted(skills.items())
    }
    result_roles = {}
    for identifier, row in sorted(roles.items()):
        skill_ids = [value for value in row["SkillIDs"].split("~") if value]
        generated = {field: row[field] for field in ROLE_FIELDS if field not in {"SkillIDs", "NormalSkillID"}}
        generated = {"SkillIDs": skill_ids, "NormalSkillID": skill_ids[0].split(":", 1)[0] if skill_ids else "", **generated}
        result_roles[identifier] = preserve_extras(old.get("roles", {}).get(identifier), generated)
    # Match the actual Role.ID. NPC rows may share the same skill basename;
    # they must not overwrite a formal hero's element, job or AI metadata.
    by_static = {
        identifier: preserve_extras(old.get("rolesByStatic", {}).get(identifier),
                                    {field: result_roles[identifier][field] for field in HERO_FIELDS})
        for identifier in sorted(hero_ids)
    }
    return {**copy.deepcopy(old), "version": provenance["version"], "source": provenance,
            "skills": result_skills, "roles": result_roles, "rolesByStatic": by_static}


def build_auto_rules(data_root: Path, roles: dict, skills: dict, raw_manifest: dict, old: dict) -> tuple[dict, dict]:
    index = read_object(data_root / "character_index.json")
    entries = index.get("characters")
    if index.get("schema_version") != "1.0" or not isinstance(entries, list):
        raise ValueError("Processed character index must use schema_version 1.0")
    formal_ids = {identifier for identifier, role in roles.items() if role["IsHero"] == "1"}
    indexed_ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    raw_ids = [entry.get("id") for entry in raw_manifest.get("characters", []) if isinstance(entry, dict)]
    if (len(indexed_ids) != len(entries) or len(set(indexed_ids)) != len(entries)
            or set(indexed_ids) != formal_ids or len(raw_ids) != len(formal_ids) or set(raw_ids) != formal_ids):
        raise ValueError("Source, raw manifest and processed character rosters differ")
    file_ids = {path.stem for path in data_root.glob("H*.json") if HERO_ID.fullmatch(path.stem)}
    if file_ids != formal_ids:
        raise ValueError("Processed character files do not match the formal source roster")
    rules, missing_skills = {}, []
    dossier_hashes = {}
    for identifier in sorted(formal_ids):
        path = data_root / f"{identifier}.json"
        record = read_object(path)
        if record.get("schema_version") != "2.0-processed" or record.get("ID") != identifier:
            raise ValueError(f"Invalid processed character dossier: {path}")
        if any(record.get(field) != roles[identifier][field] for field in ("Element", "Job")):
            raise ValueError(f"Processed character metadata differs from source Role: {identifier}")
        character_skills = record.get("Skill")
        auto_cast = record.get("AutoCast")
        if not isinstance(character_skills, list) or not isinstance(auto_cast, list):
            raise ValueError(f"Invalid Skill/AutoCast in {path}")
        expected_skills = [value.split(":", 1)[0] for value in roles[identifier]["SkillIDs"].split("~") if value]
        if [skill.get("ID") for skill in character_skills if isinstance(skill, dict)] != expected_skills:
            raise ValueError(f"Processed skill collection differs from source Role: {identifier}")
        compact_skills = []
        for skill in character_skills:
            skill_id = skill["ID"]
            previous_skill = next((value for value in old.get(identifier, {}).get("skills", [])
                                   if isinstance(value, dict) and value.get("id") == skill_id), None)
            targeting = skill.get("Targeting") or {}
            if skill_id not in skills:
                if not skill.get("missing"):
                    raise ValueError(f"Processed skill {skill_id} is absent from source without a missing marker")
                missing_skills.append(skill_id)
            else:
                if skill.get("missing") or any(targeting.get(field, "") != skills[skill_id][field]
                                               for field in ("SkillType", "TargetCamp", "TargetType")):
                    raise ValueError(f"Processed skill targeting differs from source: {skill_id}")
            generated = {
                "id": skill_id, "cooldown": skill.get("CoolDown_Final", skill.get("CoolDown")),
                "soul": skill.get("Soul_Final", skill.get("Soul")),
                "targetCamp": targeting.get("TargetCamp"), "targetType": targeting.get("TargetType"),
                "noSelf": targeting.get("NoSelf"),
            }
            compact_skills.append(preserve_extras(previous_skill, generated))
        for rule in auto_cast:
            if not isinstance(rule, dict) or type(rule.get("SkillIndex")) is not int:
                raise ValueError(f"Invalid auto-cast rule in {path}")
            position = rule["SkillIndex"]
            if not 0 <= position < len(expected_skills) or rule.get("SkillID") != expected_skills[position]:
                raise ValueError(f"Auto-cast rule refers to the wrong live skill index in {path}")
        rules[identifier] = preserve_extras(old.get(identifier), {
            "ai": roles[identifier]["AIs"], "skills": compact_skills, "autoCast": auto_cast,
        })
        dossier_hashes[identifier] = sha256(path)
    return rules, {"missing_source_skills": missing_skills, "processed_sha256": dossier_hashes,
                   "character_index_sha256": sha256(data_root / "character_index.json")}


def equipment_icons(source: Path) -> dict[str, Path]:
    directory = source / "装备" / "图标"
    if not directory.is_dir():
        raise ValueError(f"Missing equipment image directory: {directory}")
    images: dict[str, Path] = {}
    for path in sorted(directory.glob("*.png")):
        identifier = path.stem.split("_", 1)[0]
        if identifier in images and path.read_bytes() != images[identifier].read_bytes():
            raise ValueError(f"Conflicting equipment image ID: {identifier}")
        images[identifier] = path
    return images


def build_equipment(source: Path, previous_root: Path, equipment: dict, items: dict, localization: dict, old: dict) -> tuple[dict, dict]:
    images = equipment_icons(source)
    result = copy.deepcopy(old)
    diagnostics: dict[str, list] = {"missing_images": [], "missing_names": [], "legacy_names_retained": [],
                                  "historical_ids_retained": sorted(set(old) - set(equipment)), "runtime_images_retained": []}
    for identifier, row in sorted(equipment.items()):
        if row["Part"] not in SLOTS or not re.fullmatch(r"\d+", row["EquipLV"]):
            raise ValueError(f"Invalid source equipment part or level: {identifier}")
        previous = old.get(identifier) or {}
        key = items.get(identifier, {}).get("Name", "")
        localized = localization.get(key, "") if key else ""
        if not isinstance(localized, str):
            raise ValueError(f"Equipment name localization is not text: {identifier}")
        name = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", localized)).strip()
        if not name:
            name = previous.get("name") or ""
            diagnostics["legacy_names_retained" if name else "missing_names"].append(identifier)
        if identifier in images:
            image = f"assets/equip/{identifier}.png"
        else:
            image = ""
            previous_image = previous.get("img")
            if isinstance(previous_image, str) and previous_image:
                filename = previous_image.replace("\\", "/").rsplit("/", 1)[-1]
                if (previous_root / "assets" / "equip" / filename).is_file():
                    image = previous_image
                    diagnostics["runtime_images_retained"].append(identifier)
            if not image:
                diagnostics["missing_images"].append(identifier)
        result[identifier] = preserve_extras(previous, {"level": int(row["EquipLV"]), "name": name,
                                                       "slot": SLOTS[row["Part"]], "img": image})
    return dict(sorted(result.items())), diagnostics


def compare_indexes(old: dict, new: dict) -> dict:
    common = old.keys() & new.keys()
    changed = sorted(identifier for identifier in common if old[identifier] != new[identifier])
    return {"before": len(old), "after": len(new), "added": sorted(new.keys() - old.keys()),
            "removed": sorted(old.keys() - new.keys()), "changed": changed,
            "changed_fields": {identifier: sorted(key for key in old[identifier].keys() | new[identifier].keys()
                                                  if old[identifier].get(key) != new[identifier].get(key))
                               for identifier in changed if isinstance(old[identifier], dict) and isinstance(new[identifier], dict)}}


def build_game_config(tables: dict) -> dict:
    """Keep shared consumers independent of the full battle data file."""
    minimum = tables["PublicVariable"].get("GuildWarLessLV", {}).get("VALUE")
    try:
        if int(minimum) <= 0 or str(int(minimum)) != str(minimum):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Invalid shared game parameter: GuildWarLessLV") from None
    activity = tables["Activity"].get("Version_GVG")
    if not isinstance(activity, dict):
        raise ValueError("Missing shared activity definition: Version_GVG")
    return {"schema": 1, "tables": {
        "PublicVariable": {"GuildWarLessLV": {"VALUE": str(minimum)}},
        "Activity": {"Version_GVG": {"IsUnlimitTime": activity.get("IsUnlimitTime", "")}},
    }}


def build_metadata(source: Path, *, previous_root: Path = ROOT, data_root: Path | None = None,
                   source_version: str | None = None) -> tuple[dict[Path, dict], dict]:
    source, previous_root = source.resolve(), previous_root.resolve()
    data_root = data_root.resolve() if data_root is not None else source / "团员" / "资料"
    raw_manifest = read_object(data_root.parent / "资料_原始" / "manifest.json")
    provenance = source_provenance(source, raw_manifest, source_version)
    skills = load_table(source, "Skill", SKILL_FIELDS)
    roles = load_table(source, "Role", ("ID", "IsHero", *(field for field in ROLE_FIELDS if field != "NormalSkillID")))
    equipment = load_table(source, "Equipment", ("ID", "Part", "EquipLV"))
    items = load_table(source, "Item", ("ID", "Name", "ItemType"))
    localization = read_object(source / "文本" / "本地化" / "loc_CHS_FINAL.json")
    old_static = read_object(previous_root / STATIC_PATH, optional=True)
    old_ai = read_object(previous_root / AI_PATH, optional=True)
    old_equipment = read_object(previous_root / EQUIPMENT_PATH, optional=True)
    if any(not isinstance(old_static.get(field, {}), dict) for field in ("skills", "roles", "rolesByStatic")):
        raise ValueError("Previous static metadata indexes must be objects")
    if any(not isinstance(record, dict) for index in (old_ai, old_equipment) for record in index.values()):
        raise ValueError("Previous AI/equipment metadata records must be objects")
    static = build_static(skills, roles, old_static, provenance)
    auto_rules, character_evidence = build_auto_rules(data_root, roles, skills, raw_manifest, old_ai)
    references, equipment_evidence = build_equipment(source, previous_root, equipment, items, localization, old_equipment)
    battle, battle_evidence = build_battle_metadata(source, provenance)
    outputs = {STATIC_PATH: static, AI_PATH: auto_rules, EQUIPMENT_PATH: references,
               BATTLE_PATH: battle, GAME_CONFIG_PATH: build_game_config(battle["tables"])}
    report = {"schema": 1, "source_root": str(source), "character_data_root": str(data_root), "source": provenance,
              "character_evidence": character_evidence, "equipment": equipment_evidence, "battle": battle_evidence,
              "differences": {"skills": compare_indexes(old_static.get("skills", {}), static["skills"]),
                              "roles": compare_indexes(old_static.get("roles", {}), static["roles"]),
                              "rolesByStatic": compare_indexes(old_static.get("rolesByStatic", {}), static["rolesByStatic"]),
                              "auto_cast_rules": compare_indexes(old_ai, auto_rules),
                              "equipment": compare_indexes(old_equipment, references)}}
    return outputs, report


def encode_json(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def write_atomic(path: Path, payload: bytes, backup_root: Path | None = None) -> bool:
    if path.exists() and path.read_bytes() == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and backup_root is not None:
        previous_hash = sha256(path)
        backup = backup_root / previous_hash / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists() and sha256(backup) != previous_hash:
            raise ValueError(f"Conflicting metadata backup: {backup}")
        if not backup.exists():
            shutil.copy2(path, backup)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return True


def write_metadata(outputs: dict[Path, dict], report: dict, output_root: Path, *,
                   report_path: Path | None = None) -> dict:
    # Build and serialize everything first; invalid source snapshots never
    # partially replace runtime indexes. Each actual file replacement is atomic.
    encoded = {relative: encode_json(payload) for relative, payload in outputs.items()}
    output_root = output_root.resolve()
    backup_root = output_root / "build" / "game-metadata-backups"
    changed = []
    for relative, payload in encoded.items():
        if write_atomic(output_root / relative, payload, backup_root):
            changed.append(relative.as_posix())
    report["outputs"] = {relative.as_posix(): {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
                         for relative, payload in encoded.items()}
    # Deliberately omit timestamps and write counts so repeated identical
    # input leaves both the generated JSON and report unchanged.
    report_path = report_path or output_root / "build" / "game-metadata-refresh.json"
    write_atomic(report_path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {"version": report["source"]["version"], "skills": len(outputs[STATIC_PATH]["skills"]),
            "roles": len(outputs[STATIC_PATH]["roles"]), "auto_cast_roles": len(outputs[AI_PATH]),
            "equipment": len(outputs[EQUIPMENT_PATH]), "changed_files": changed, "report": str(report_path)}


def sync(source: Path, output_root: Path, *, previous_root: Path = ROOT, data_root: Path | None = None,
         source_version: str | None = None, report_path: Path | None = None) -> dict:
    outputs, report = build_metadata(source, previous_root=previous_root, data_root=data_root, source_version=source_version)
    return write_metadata(outputs, report, output_root, report_path=report_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "assets_full")
    parser.add_argument("--output-root", type=Path, default=ROOT)
    parser.add_argument("--previous-root", type=Path, default=ROOT, help="existing metadata and optimized equipment images")
    parser.add_argument("--data-root", type=Path, help="processed Ark2 dossiers; defaults to source/团员/资料")
    parser.add_argument("--source-version", help="optional catalog/version label; otherwise use extraction report or table hashes")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = sync(args.source, args.output_root, previous_root=args.previous_root, data_root=args.data_root,
                      source_version=args.source_version, report_path=args.report)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Game metadata refresh failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
