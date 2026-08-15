"""Map a local full resource archive to LucimaTools runtime assets."""

from __future__ import annotations

import argparse
import ast
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
    }
    _validate_target(target, set(selection))
    for category, files in selection.items():
        _sync_category(target, category, files)
    counts = {category: len(files) for category, files in selection.items()}
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
