"""Build cloud data, media and optional finished cards with one local command."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

from build_cloud_encyclopedia import build as build_cloud
from build_encyclopedia import build_bundle
from encyclopedia_release import PLAN_NAME, collect_release, read, save_plan
from stage_encyclopedia_cdn import VERSION, atomic_write, json_bytes, stage_cdn


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORK = ROOT / "build/encyclopedia"
DEFAULT_OUTPUT = ROOT / "releases/encyclopedia"


def prepare_avatars(ark2_root, bundle):
    """Use the current extraction directly, including newly introduced heroes."""
    source = ark2_root / "out/assets/团员/头像"
    if not source.is_dir():
        raise ValueError("Avatar source missing: " + str(source))
    avatars = {}
    for directory in sorted(source.iterdir()):
        match = re.fullmatch(r"(H\d{3})(?:_.*)?", directory.name)
        if not directory.is_dir() or not match:
            continue
        hero = match[1]
        image = directory / f"Icon_Head_S_{hero}.png"
        if not image.is_file():
            raise ValueError("Avatar image missing: " + str(image))
        if hero in avatars and avatars[hero].read_bytes() != image.read_bytes():
            raise ValueError("Conflicting avatar sources: " + hero)
        avatars[hero] = image
    target = bundle.parent / "avatars"
    target.mkdir(parents=True, exist_ok=True)
    missing = []
    for row in read(bundle / "index.json")["characters"]:
        hero = row["id"]
        if hero in avatars:
            shutil.copy2(avatars[hero], target / f"{hero}.png")
        else:
            # A reused working directory must not retain an obsolete avatar.
            (target / f"{hero}.png").unlink(missing_ok=True)
            missing.append(hero)
    return target, missing


def build_release(args):
    pool = args.output.resolve()
    if args.from_existing:
        release = collect_release(pool, version=args.version, cards=args.cards)
    else:
        if not args.version or not VERSION.fullmatch(args.version):
            raise ValueError("Specify a new resource version with --version")
        source = args.ark2_root.resolve()
        work = args.work_dir.resolve() / args.version
        bundle = work / "bundle/encyclopedia"
        print("[1/4] 整理角色资料与头像", flush=True)
        build_bundle(source, bundle)
        avatars, missing = prepare_avatars(source, bundle)
        if missing:
            print("源资料缺少头像：" + ", ".join(missing), flush=True)
        print("[2/4] 整理图鉴媒体" + ("（复用现有媒体清单）" if args.reuse_media else ""), flush=True)
        if not args.reuse_media:
            report = stage_cdn(source, pool, args.version)
            atomic_write(work / "media.json", json_bytes(report))
        print("[3/4] 生成云端资料", flush=True)
        report = build_cloud(bundle, pool, avatars, args.version)
        atomic_write(work / "data.json", json_bytes(report))
        if args.cards:
            from build_encyclopedia_cards import build as build_cards
            print("[4/4] 生成成品卡片与查询表", flush=True)
            build_cards(SimpleNamespace(version=args.version, output=pool / "cards", resources=pool,
                                        bundle=bundle, index_only=False, characters=None))
        else:
            print("[4/4] 保留现有成品卡片，本次不发布卡片", flush=True)
        release = collect_release(pool, version=args.version, cards=args.cards)
    save_plan(release)
    print(f"图鉴构建校验通过：{release.counts['characters']} 名角色，{len(release.files)} 个不可变文件", flush=True)
    print("发布计划：" + str(pool / PLAN_NAME), flush=True)
    print("本地构建完成；未上传云端。", flush=True)
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="New resource version; independent of the application version")
    parser.add_argument("--ark2-root", type=Path, default=ROOT.parent / "ark2")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--cards", action="store_true", help="Also build and publish the full finished-card collection")
    parser.add_argument("--reuse-media", action="store_true", help="Reuse the existing media manifest for a data-only update")
    parser.add_argument("--from-existing", action="store_true", help="Only validate existing output and regenerate its publication plan")
    args = parser.parse_args()
    if args.from_existing and args.reuse_media:
        parser.error("--from-existing already reuses existing output")
    try:
        build_release(args)
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        raise SystemExit("图鉴构建失败：" + str(exc)) from None


if __name__ == "__main__":
    main()
