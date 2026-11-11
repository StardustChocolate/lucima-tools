"""Stage Ark2's normalized encyclopedia resources for external media loading.

This command only prepares a local directory. It validates Spine atlas page
references before copying skeletons, atlases, and their texture pages.
Resource hosting is configured separately from this local staging command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from encyclopedia_catalog import HERO_ID, load_character_records


ICON_SPRITE = re.compile(r"Icon_Skill_[A-Za-z0-9_]+\Z")
PAGE_NAME = re.compile(r".+\.(?:png|jpg|jpeg|webp)\Z", re.IGNORECASE)
FRAMING_RULE = "whole-animation-visible-rgba-60fps-v1"


def validate_framing(path: Path, source_dir: Path, hero: str, pages: list[str], portrait: Path,
                     ark2_root: Path) -> None:
    """Publish a computed camera only with the exact inputs and PNG it frames."""
    profile = json.loads(path.read_text(encoding="utf-8"))
    sources = profile.get("sources", {})
    names = {f"{hero}.skel", f"{hero}.atlas", *pages}
    bounds = profile.get("bounds", {})
    if (profile.get("schema") != 1 or profile.get("rule") != FRAMING_RULE
            or profile.get("base") != hero or profile.get("skin") != "LV1"
            or profile.get("animation") != "00_Idle" or set(sources) != names
            or any(not isinstance(bounds.get(key), (int, float)) or not math.isfinite(bounds[key])
                   for key in ("centerX", "centerY", "width", "height"))
            or bounds["width"] <= 0 or bounds["height"] <= 0):
        raise ValueError(f"{hero} 的立绘取景记录无效或规则过期")
    for name, record in sources.items():
        source = source_dir / name
        if record != {"bytes": source.stat().st_size, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}:
            raise ValueError(f"{hero} 的立绘取景记录与素材不一致：{name}")
    for field, source in (
        ("static_sha256", portrait),
        ("helper_sha256", ark2_root / "out/assets/spine/portrait-framing.js"),
        ("runtime_sha256", ark2_root / "out/assets/spine/spine-webgl.js"),
    ):
        if hashlib.sha256(source.read_bytes()).hexdigest() != profile.get(field):
            raise ValueError(f"{hero} 的立绘取景记录与 {field} 不一致，请重新生成立绘")


def atlas_pages(path: Path) -> list[str]:
    """Return the image pages declared by a Spine text atlas.

    A page header is followed by a ``size:`` line.  Looking for that pair
    avoids treating attachment names that happen to end in ``.png`` as pages.
    """
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    pages: list[str] = []
    for index, line in enumerate(lines[:-1]):
        name = line.strip()
        if not PAGE_NAME.fullmatch(name) or not lines[index + 1].strip().lower().startswith("size:"):
            continue
        if name not in pages:
            pages.append(name)
    return pages


def safe_page_path(page: str) -> bool:
    path = Path(page)
    return bool(page) and "\x00" not in page and not path.is_absolute() and not path.drive and ".." not in path.parts


def spine_runtime_source(ark2_root: Path) -> Path | None:
    runtime = ark2_root / "out" / "assets" / "spine" / "spine-webgl.js"
    return runtime if runtime.is_file() else None


def missing_skill_sources(ark2_root: Path, document: Path | None) -> dict[str, str]:
    """Allow only exact, absent skill images documented for this catalog."""
    if document is None:
        return {}
    report = json.loads(document.read_text(encoding="utf-8"))
    version = report.get("catalog_version")
    if type(version) is not int or version <= 0:
        raise ValueError("缺图清单缺少 catalog_version")
    catalog = ark2_root / f"tools/work/catalog/catalog_{version}.json"
    if hashlib.sha256(catalog.read_bytes()).hexdigest() != report.get("catalog_sha256"):
        raise ValueError("缺图清单与当前 catalog 不一致")
    result = {}
    prefix = "/out/assets/团员/技能图标/"
    for row in report["missing_images"]:
        path, reason = row["path"], row["reason"]
        sprite = path.removeprefix(prefix).removesuffix(".png")
        if not ICON_SPRITE.fullmatch(sprite) or path != prefix + sprite + ".png" \
                or not isinstance(reason, str) or not reason.strip() or sprite in result:
            raise ValueError("无效的技能缺图记录")
        if (ark2_root / "out/assets/团员/技能图标" / (sprite + ".png")).exists():
            raise ValueError("缺图清单中的图片已经存在：" + sprite)
        result[sprite] = reason
    return result


def resource_sources(
    ark2_root: Path,
    character_ids: Iterable[str] | None = None,
    source_gaps: Path | None = None,
) -> tuple[dict[str, Path], dict[str, int]]:
    source_root = ark2_root / "out" / "assets" / "团员"
    render_profile_path = source_root / "立绘/_render_profile.json"
    render_profile = json.loads(render_profile_path.read_text(encoding="utf-8")) if render_profile_path.is_file() else {}
    require_framing = render_profile.get("bounds") == FRAMING_RULE
    characters = load_character_records(ark2_root)
    if character_ids is not None:
        selected = set(character_ids)
        if not selected or any(not isinstance(hero_id, str) or not HERO_ID.fullmatch(hero_id) for hero_id in selected):
            raise ValueError("必须指定一个或多个 Hxxx 格式的角色 ID")
        unknown = sorted(selected - {entry["id"] for entry, _record in characters})
        if unknown:
            raise ValueError(f"角色索引中不存在：{', '.join(unknown)}")
        characters = [(entry, record) for entry, record in characters if entry["id"] in selected]

    portraits: list[Path] = []
    sprites: set[str] = set()
    spine_sources: dict[str, tuple[Path, list[str]]] = {}
    for entry, record in characters:
        hero_id = entry["id"]
        portrait = source_root / "立绘" / f"{hero_id}.png"
        if entry["hasStaticPortrait"]:
            portraits.append(portrait)
        if entry["hasSpine"]:
            source_dir = source_root / "动态立绘" / hero_id
            atlas = source_dir / f"{hero_id}.atlas"
            pages = atlas_pages(atlas)
            if not pages:
                raise ValueError(f"{hero_id} 的 atlas 没有纹理页")
            unsafe = [page for page in pages if not safe_page_path(page)]
            missing_pages = [page for page in pages if not (source_dir / Path(page)).is_file()]
            if unsafe:
                raise ValueError(f"{hero_id} 的 atlas 包含非法纹理路径：{unsafe!r}")
            if missing_pages:
                raise FileNotFoundError(f"{hero_id} 缺少 atlas 纹理页：{missing_pages!r}")
            spine_sources[hero_id] = (source_dir, pages)
        skills = record.get("Skill")
        if not isinstance(skills, list):
            raise ValueError(f"{hero_id} 缺少技能数组")
        for skill in skills:
            if not isinstance(skill, dict) or not isinstance(skill.get("IconSprite"), str):
                raise ValueError(f"{hero_id} 有技能缺少 IconSprite")
            sprite = skill["IconSprite"]
            if sprite:
                if not ICON_SPRITE.fullmatch(sprite):
                    raise ValueError(f"{hero_id} 有无效技能图标标识：{sprite!r}")
                sprites.add(sprite)

    icon_source = source_root / "技能图标"
    documented = missing_skill_sources(ark2_root, source_gaps)
    if character_ids is None and set(documented) - sprites:
        raise ValueError("缺图清单包含当前角色未引用的技能图片")
    missing = sorted(sprite for sprite in sprites if not (icon_source / f"{sprite}.png").is_file() and sprite not in documented)
    if missing:
        raise FileNotFoundError(f"{len(missing)} 个技能图标未归集：{', '.join(missing[:8])}")
    # Keep the complete release's historical behavior. A partial publication
    # contains only the selected characters' references, never every icon.
    icon_files = (
        sorted(icon_source.glob("*.png"))
        if character_ids is None
        else [icon_source / f"{sprite}.png" for sprite in sorted(sprites) if sprite not in documented]
    )
    for source in icon_files:
        if not ICON_SPRITE.fullmatch(source.stem):
            raise ValueError(f"无效规范化技能图标文件名：{source.name!r}")
    runtime = spine_runtime_source(ark2_root)
    if spine_sources and runtime is None:
        raise FileNotFoundError("存在 Spine 角色，但找不到 spine-webgl.js 运行库")
    sources: dict[str, Path] = {}
    for source in portraits:
        sources[f"portraits/static/{source.name}"] = source
    for source in icon_files:
        sources[f"skills/{source.name}"] = source
    spine_files = 0
    for hero_id, (source_dir, pages) in sorted(spine_sources.items()):
        for suffix in (".skel", ".atlas"):
            sources[f"portraits/spine/{hero_id}/{hero_id}{suffix}"] = source_dir / f"{hero_id}{suffix}"
        for page in pages:
            sources[f"portraits/spine/{hero_id}/{Path(page).as_posix()}"] = source_dir / Path(page)
        spine_files += 2 + len(pages)
        framing = source_root / "立绘" / f"{hero_id}.framing.json"
        if require_framing and not framing.is_file():
            raise ValueError(f"{hero_id} 的新版静态立绘缺少配对镜头记录")
        if framing.is_file():
            validate_framing(framing, source_dir, hero_id, pages,
                             source_root / "立绘" / f"{hero_id}.png", ark2_root)
            if require_framing:
                record = render_profile.get("portraits", {}).get(hero_id, {})
                if hashlib.sha256(framing.read_bytes()).hexdigest() != record.get("framing_sha256"):
                    raise ValueError(f"{hero_id} 的镜头记录与生成记录不一致")
            sources[f"portraits/spine/{hero_id}/{hero_id}.framing.json"] = framing
            spine_files += 1

    if runtime is not None and spine_sources:
        sources["runtime/spine-webgl.js"] = runtime
    return sources, {
        "characters": len(characters),
        "portraits": len(portraits),
        "skill_icons": len(icon_files),
        "spine": len(spine_sources),
        "spine_files": spine_files,
        "spine_runtime": int(runtime is not None and bool(spine_sources)),
    }


def stage_resources(
    ark2_root: Path,
    output_root: Path,
    character_ids: Iterable[str] | None = None,
    source_gaps: Path | None = None,
) -> dict[str, int]:
    sources, counts = resource_sources(ark2_root, character_ids, source_gaps)
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"资源输出目录非空，请指定新的版本目录：{output_root}")
    for relative, source in sources.items():
        target = output_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ark2-root", type=Path, default=Path(r"E:\Project\ark2"))
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "build" / "encyclopedia-resources",
    )
    parser.add_argument(
        "--character", dest="characters", action="append", metavar="Hxxx",
        help="只暂存指定角色及其引用的技能图标；可重复指定，省略时保持全量发布",
    )
    parser.add_argument("--source-gaps", type=Path, help="Catalog-bound official missing skill image report")
    args = parser.parse_args()
    print(json.dumps(stage_resources(args.ark2_root, args.output, args.characters, args.source_gaps), ensure_ascii=False))


if __name__ == "__main__":
    main()
