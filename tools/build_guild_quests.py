"""Build the runtime guild weekly quest catalog from extracted game tables."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path("assets/guild_quests.json")


def build(source: Path) -> dict:
    def table(name, fields):
        with (source / "文本/数据表" / f"sd_{name}.txt").open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="@")
            if not set(fields).issubset(reader.fieldnames or []):
                raise ValueError(f"Invalid {name} table header")
            return list(reader)

    loc = json.loads((source / "文本/本地化/loc_CHS_FINAL.json").read_text(encoding="utf-8-sig"))
    items = {row["ID"]: row for row in table("Item", ("ID", "Name"))}
    drops = table("Drop", ("DropID", "TagString", "MinItemCount", "MaxItemCount", "Rate"))
    personal, tiers = {}, {}
    for row in table("Quest", ("ID", "Name", "Tags", "ConditionCount", "Drops", "GuildDrops")):
        tags = row["Tags"].split("~")
        if row.get("Group") != "Guild" or not {"GuildQuest", "GuildPoint"}.intersection(tags):
            continue
        target = int(row["ConditionCount"])
        if target <= 0:
            raise ValueError(f"Invalid guild quest target: {row['ID']}")
        if "GuildQuest" in tags:
            name = loc.get(row["Name"])
            if not isinstance(name, str) or not name.strip() or "\ufffd" in name:
                raise ValueError(f"Missing guild quest name: {row['ID']}")
            points = 0
            for drop in drops:
                if drop["DropID"] != row["GuildDrops"] or drop["TagString"] != "82":
                    continue
                if (float(drop["Rate"]) != 1 or drop["MinItemCount"] != drop["MaxItemCount"]
                        or drop.get("Group") or drop.get("DropFun")):
                    raise ValueError(f"Unsupported guild quest points: {row['ID']}")
                count = int(drop["MinItemCount"])
                if count <= 0:
                    raise ValueError(f"Invalid guild quest points: {row['ID']}")
                points += count
            personal[row["ID"]] = {"name": name, "target": target, "points": points}
        else:
            rewards = []
            for drop in drops:
                if drop["DropID"] != row["Drops"]:
                    continue
                item = drop["TagString"]
                if (item not in items or float(drop["Rate"]) != 1
                        or drop["MinItemCount"] != drop["MaxItemCount"]):
                    raise ValueError(f"Unsupported guild tier drop: {row['ID']}")
                name = loc.get(items[item]["Name"])
                count = int(drop["MinItemCount"])
                if not isinstance(name, str) or count <= 0:
                    raise ValueError(f"Invalid guild tier reward: {row['ID']}")
                rewards.append({"item": item, "name": name, "count": count})
            if not rewards:
                raise ValueError(f"Missing guild tier reward: {row['ID']}")
            tiers[row["ID"]] = {"target": target, "rewards": rewards}
    if not personal or not tiers:
        raise ValueError("Empty guild weekly catalog")
    return {"schema": 1, "source": {name: hashlib.sha256(
                (source / "文本/数据表" / f"sd_{name}.txt").read_bytes()).hexdigest()
            for name in ("Quest", "Drop", "Item")}, "personal": personal, "tiers": tiers}


def write(target: Path, data: dict):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "assets_full")
    parser.add_argument("--target", type=Path, default=ROOT / TARGET)
    args = parser.parse_args()
    data = build(args.source)
    write(args.target, data)
    print(f"Guild weekly catalog: {len(data['personal'])} quests, {len(data['tiers'])} tiers -> {args.target}")
