"""Manually refresh runtime assets from assets_full in a source checkout."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets_full"


def main() -> int:
    if not SOURCE.is_dir():
        print("assets_full not found; keeping checked-in runtime assets")
        return 0

    sys.path.insert(0, str(ROOT))
    from tools import build_guild_aid, build_guild_quests, build_guild_store, sync_assets, sync_commodity_catalog, sync_game_metadata

    try:
        # Validate the commodity table before the asset sync writes any files.
        catalog = sync_commodity_catalog.read_catalog(
            SOURCE / "文本" / "数据表" / "sd_Commodity.txt",
        )
        guild_aid = build_guild_aid.build(SOURCE)
        guild_quests = build_guild_quests.build(SOURCE)
        guild_store = build_guild_store.build(SOURCE)
        game_metadata, metadata_report = sync_game_metadata.build_metadata(SOURCE, previous_root=ROOT)
        equipment_references = game_metadata[sync_game_metadata.EQUIPMENT_PATH]
        # Check images and inventory indexes against the NEW equipment roster
        # before any metadata or asset file is replaced.
        sync_assets.sync(
            SOURCE,
            ROOT / "assets",
            items_target=ROOT / "assets" / "items",
            metadata_target=ROOT / "backend",
            preserve_existing_images=True,
            equipment_references=equipment_references,
            validate_only=True,
        )
        metadata_counts = sync_game_metadata.write_metadata(game_metadata, metadata_report, ROOT)
        counts, missing_avatars = sync_assets.sync(
            SOURCE,
            ROOT / "assets",
            items_target=ROOT / "assets" / "items",
            metadata_target=ROOT / "backend",
            preserve_existing_images=True,
            equipment_references=equipment_references,
        )
        sync_commodity_catalog.write_catalog(
            ROOT / "backend" / "commodity_catalog.json", catalog,
        )
        build_guild_aid.write(ROOT / build_guild_aid.TARGET, guild_aid)
        build_guild_quests.write(ROOT / build_guild_quests.TARGET, guild_quests)
        build_guild_store.write(ROOT / build_guild_store.TARGET, guild_store)
        build_guild_store.sync_icons(SOURCE, ROOT / 'assets/items', guild_store)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Asset sync failed: {exc}", file=sys.stderr)
        return 1
    counts["commodities"] = len(catalog)
    counts["guild_aid_items"] = len(guild_aid["items"])
    counts["guild_weekly_quests"] = len(guild_quests["personal"])
    counts["guild_store_goods"] = len(guild_store["goods"])
    counts["skill_static"] = metadata_counts["skills"]
    counts["auto_cast_roles"] = metadata_counts["auto_cast_roles"]
    counts["equipment_metadata"] = metadata_counts["equipment"]
    print("Synced " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    if missing_avatars:
        print("Warning: no source avatar for known hero IDs: "
              + ", ".join(missing_avatars), file=sys.stderr)
    print("Restart the app or test backend to reload resource indexes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
