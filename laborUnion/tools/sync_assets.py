"""Map a local full resource archive to LucimaTools runtime assets."""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "assets_full"
DEFAULT_TARGET = ROOT / "assets"

SOURCE_PATHS = {
    "avatars": Path("团员") / "头像",
    "equip": Path("装备") / "图标",
    "sets": Path("装备") / "套装图标",
    "stats": Path("界面图标") / "能力",
    "slots": Path("界面图标") / "装备部位",
    "ui": Path("界面图标"),
    "localization": Path("文本") / "本地化",
}

LOCALIZATION_SOURCE_FILE = "loc_CHS_FINAL.json"
ITEM_TABLE_SOURCE = Path("文本") / "数据表" / "sd_Item.txt"
ROLE_TABLE_SOURCE = Path("文本") / "数据表" / "sd_Role.txt"

ITEM_CHARM_TYPES = {
    "WeaponCharm", "HeadCharm", "BodyCharm", "NecklaceCharm", "RingCharm",
    "ShoesCharm", "ArtifactCharm", "EquipCharm", "AccessoryCharm",
}

STAT_SOURCE_FILES = {
    "Attack.png": "Attack_攻击力.png",
    "Critical.png": "Critical_暴击率.png",
    "CriticalDamage.png": "CriticalDamage_暴击伤害.png",
    "Defense.png": "Defence_防御力.png",
    "EffectHit.png": "EffectHit_状态命中.png",
    "Health.png": "HP_生命力.png",
    "Resistance.png": "Resistance_状态抗性.png",
    "Speed.png": "Speed_速度.png",
}

ATTRIBUTE_SOURCE_FILES = {
    "fire.png": "Fire_火属性.png",
    "water.png": "Ice_水属性.png",
    "wood.png": "Earth_木属性.png",
    "light.png": "Light_光属性.png",
    "dark.png": "Dark_暗属性.png",
}

SLOT_SOURCE_FILES = {
    "weapon.png": "Weapon_UIz_Icon_Weapon.png",
    "helmet.png": "Helmet_UIz_Icon_Helmet.png",
    "armor.png": "Armor_UIz_Icon_Armor.png",
    "necklace.png": "Necklace_UIz_Icon_Necklace.png",
    "ring.png": "Ring_UIz_Icon_Ring.png",
    "boots.png": "Boot_UIz_Icon_Boot.png",
}

UI_SOURCE_FILES = {
    "hunt.png": Path("其它") / "Episode" / "Hunt.png",
    "element.png": Path("其它") / "Episode" / "Elf.png",
    "mail.png": Path("其它") / "UI_Lobby" / "UI05_Btn_C_Mail.png",
    "customized-equip.png": Path("其它") / "UI_Lobby" / "UI05_Icon_EQuip.png",
    "equipment.png": Path("其它") / "UIz" / "UIz_Icon_Equip.png",
    "activity.png": Path("菜单模块") / "Lobby_SideStory_活动故事.png",
    "store.png": Path("菜单模块") / "Lobby_Shop_商店.png",
    "hero-all.png": Path("其它") / "UIz" / "UIz_Icon_All.png",
}


def _set_icon_map() -> dict[str, str]:
    tree = ast.parse((ROOT / "backend" / "tasks.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "SET_ICON" for target in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("backend/tasks.py does not define a literal SET_ICON mapping")


def _equipment_ids() -> set[str]:
    with (ROOT / "backend" / "equip_ref.json").open(encoding="utf-8") as stream:
        equipment = json.load(stream)
    return {
        Path(row["img"]).stem
        for row in equipment.values()
        if row.get("img")
    }


def _known_avatar_ids() -> set[str]:
    with (ROOT / "backend" / "item_names.json").open(encoding="utf-8") as stream:
        item_names = json.load(stream)
    return {item_id for item_id in item_names if re.fullmatch(r"H\d+", item_id)}


def _prefixed_pngs(directory: Path) -> dict[str, Path]:
    if not directory.is_dir():
        raise RuntimeError(f"missing source directory: {directory}")

    index: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".png":
            continue
        resource_id = path.stem.split("_", 1)[0]
        if resource_id in index:
            raise RuntimeError(
                f"duplicate resource ID {resource_id}: {index[resource_id]} and {path}"
            )
        index[resource_id] = path
    return index


def _avatar_files(source: Path) -> tuple[dict[str, Path], list[str]]:
    avatar_root = source / SOURCE_PATHS["avatars"]
    if not avatar_root.is_dir():
        raise RuntimeError(f"missing source directory: {avatar_root}")

    files: dict[str, Path] = {}
    for directory in sorted(avatar_root.iterdir()):
        if not directory.is_dir():
            continue
        match = re.fullmatch(r"(H\d+)(?:_.*)?", directory.name)
        if not match:
            raise RuntimeError(f"invalid avatar directory name: {directory}")
        hero_id = match.group(1)
        source_file = directory / f"Icon_Head_S_{hero_id}.png"
        if not source_file.is_file():
            raise RuntimeError(f"missing small avatar image: {source_file}")
        output_name = f"{hero_id}.png"
        if output_name in files:
            # Resource archives may contain both an unnamed and a localized
            # directory for the same hero.  Ignore only byte-identical copies;
            # a real collision must still fail instead of choosing silently.
            if files[output_name].read_bytes() == source_file.read_bytes():
                continue
            raise RuntimeError(f"duplicate avatar ID {hero_id}: {directory}")
        files[output_name] = source_file

    if not files:
        raise RuntimeError(f"no avatar directories found in: {avatar_root}")
    missing_known = sorted(_known_avatar_ids() - {Path(name).stem for name in files})
    return files, missing_known


def _equipment_files(source: Path) -> dict[str, Path]:
    index = _prefixed_pngs(source / SOURCE_PATHS["equip"])
    required_ids = _equipment_ids()
    missing = sorted(required_ids - index.keys())
    if missing:
        raise RuntimeError(
            f"full resource archive is missing {len(missing)} required equipment images: "
            + ", ".join(missing)
        )
    return {f"{resource_id}.png": index[resource_id] for resource_id in required_ids}


def _set_files(source: Path) -> dict[str, Path]:
    index = _prefixed_pngs(source / SOURCE_PATHS["sets"])
    set_icons = _set_icon_map()
    missing = sorted(set(set_icons) - index.keys())
    if missing:
        raise RuntimeError(
            f"full resource archive is missing {len(missing)} required set images: "
            + ", ".join(missing)
        )
    return {
        f"{output_id}.png": index[set_id]
        for set_id, output_id in set_icons.items()
    }


def _stat_files(source: Path) -> dict[str, Path]:
    stat_root = source / SOURCE_PATHS["stats"]
    files = {output: stat_root / source_name for output, source_name in STAT_SOURCE_FILES.items()}
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise RuntimeError("full resource archive is missing stat images: " + ", ".join(missing))
    return files


def _attribute_files(source: Path) -> dict[str, Path]:
    attribute_root = source / "界面图标" / "属性"
    files = {output: attribute_root / source_name for output, source_name in ATTRIBUTE_SOURCE_FILES.items()}
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise RuntimeError("full resource archive is missing attribute images: " + ", ".join(missing))
    return files


def _slot_files(source: Path) -> dict[str, Path]:
    slot_root = source / SOURCE_PATHS["slots"]
    files = {output: slot_root / source_name for output, source_name in SLOT_SOURCE_FILES.items()}
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise RuntimeError("full resource archive is missing slot images: " + ", ".join(missing))
    return files


def _ui_files(source: Path) -> dict[str, Path]:
    ui_root = source / SOURCE_PATHS["ui"]
    files = {output: ui_root / source_path for output, source_path in UI_SOURCE_FILES.items()}
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise RuntimeError("full resource archive is missing UI images: " + ", ".join(missing))
    return files


def _localization_files(source: Path) -> dict[str, Path]:
    source_file = source / SOURCE_PATHS["localization"] / LOCALIZATION_SOURCE_FILE
    if not source_file.is_file():
        raise RuntimeError(f"full resource archive is missing localization file: {source_file}")
    try:
        with source_file.open(encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid localization file: {source_file}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"localization file must contain an object: {source_file}")
    return {LOCALIZATION_SOURCE_FILE: source_file}


def _item_category(row: dict[str, str]) -> str:
    item_id = (row.get("ID") or "").strip()
    item_type = (row.get("ItemType") or "").strip()
    if item_type in ITEM_CHARM_TYPES:
        return "enhancement"
    if item_type == "Rune":
        return "element"
    if item_type in {"Crafting", "ReforgeMat"} and item_id.startswith("CR"):
        return "crystal"
    if item_type == "ActivityCurrency" and item_id.startswith("AH"):
        return "activity"
    if item_type == "Catalyst":
        return "catalyst"
    if item_id.isdigit():
        return "wealth"
    return "other"


def _item_categories(source: Path) -> dict[str, str]:
    source_file = source / ITEM_TABLE_SOURCE
    if not source_file.is_file():
        raise RuntimeError(f"full resource archive is missing item table: {source_file}")
    try:
        # 角色表的说明字段可能混有旧版本地编码；ID / Element 为 ASCII，替换
        # 无关字段中的坏字节不会影响分类索引生成。
        with source_file.open(encoding="utf-8", errors="replace", newline="") as stream:
            rows = csv.DictReader(stream, delimiter="@")
            if not rows.fieldnames or "ID" not in rows.fieldnames or "ItemType" not in rows.fieldnames:
                raise RuntimeError(f"invalid item table header: {source_file}")
            categories = {
                item_id: _item_category(row)
                for row in rows
                if (item_id := (row.get("ID") or "").strip())
            }
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RuntimeError(f"invalid item table: {source_file}: {exc}") from exc
    return categories


def _write_item_categories(target: Path, categories: dict[str, str]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    output = target / "item_categories.json"
    output.write_text(
        json.dumps(categories, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _role_elements(source: Path) -> dict[str, str]:
    source_file = source / ROLE_TABLE_SOURCE
    if not source_file.is_file():
        raise RuntimeError(f"full resource archive is missing role table: {source_file}")
    try:
        with source_file.open(encoding="utf-8", errors="replace", newline="") as stream:
            rows = csv.DictReader(stream, delimiter="@")
            if not rows.fieldnames or "ID" not in rows.fieldnames or "Element" not in rows.fieldnames:
                raise RuntimeError(f"invalid role table header: {source_file}")
            elements = {
                role_id: element
                for row in rows
                if (role_id := (row.get("ID") or "").strip())
                if (element := (row.get("Element") or "").strip())
            }
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RuntimeError(f"invalid role table: {source_file}: {exc}") from exc
    return elements


def _write_role_elements(target: Path, elements: dict[str, str]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    output = target / "role_elements.json"
    output.write_text(
        json.dumps(elements, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _validate_target(target: Path, categories: set[str]) -> None:
    for category in categories:
        target_dir = target / category
        if not target_dir.exists():
            continue
        unexpected_dirs = [path for path in target_dir.iterdir() if path.is_dir()]
        if unexpected_dirs:
            raise RuntimeError(f"unexpected directory in managed target: {unexpected_dirs[0]}")


def _sync_category(target: Path, category: str, files: dict[str, Path]) -> None:
    target_dir = target / category
    target_dir.mkdir(parents=True, exist_ok=True)
    for path in target_dir.iterdir():
        if path.is_file() and path.name not in files:
            path.unlink()
    for output_name, source_file in sorted(files.items()):
        shutil.copy2(source_file, target_dir / output_name)


def sync(source: Path, target: Path) -> tuple[dict[str, int], list[str]]:
    source = source.resolve()
    target = target.resolve()
    avatar_files, missing_known_avatars = _avatar_files(source)
    selection = {
        "avatars": avatar_files,
        "equip": _equipment_files(source),
        "sets": _set_files(source),
        "stats": _stat_files(source),
        "attributes": _attribute_files(source),
        "slots": _slot_files(source),
        "ui": _ui_files(source),
        "localization": _localization_files(source),
    }
    _validate_target(target, set(selection))
    for category, files in selection.items():
        _sync_category(target, category, files)
    counts = {category: len(files) for category, files in selection.items()}
    item_categories = _item_categories(source)
    _write_item_categories(target, item_categories)
    counts["item_categories"] = len(item_categories)
    role_elements = _role_elements(source)
    _write_role_elements(target, role_elements)
    counts["role_elements"] = len(role_elements)
    return counts, missing_known_avatars


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="full resource archive root")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="runtime asset output directory")
    args = parser.parse_args()

    try:
        counts, missing_known_avatars = sync(args.source, args.target)
    except RuntimeError as exc:
        parser.error(str(exc))
    print("Synced " + ", ".join(f"{category}={count}" for category, count in counts.items()))
    if missing_known_avatars:
        print(
            "Warning: no source avatar for known hero IDs: "
            + ", ".join(missing_known_avatars),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
