"""Extract the small guild-aid catalog from the game's item tags and aid rules."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path("assets/guild_aid.json")
GROUPS = {"CatalystRare": "catalyst", "Rune1": "element", "Rune2": "element",
          "Mat1": "hunt", "Mat2": "hunt", "Mat3": "hunt", "Mat4": "hunt"}


def build(source: Path) -> dict:
    def table(name, fields):
        path = source / "文本/数据表" / f"sd_{name}.txt"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="@")
            if not set(fields).issubset(reader.fieldnames or []):
                raise ValueError(f"Invalid {name} table header")
            rows = list(reader)
        if len({r["ID"] for r in rows}) != len(rows):
            raise ValueError(f"Duplicate IDs in {name}")
        return rows

    rules = {}
    for row in table("GuildAidItem", ("ID", "MaxCount", "PerSupportCount", "RewardCount")):
        if row["ID"] not in GROUPS:
            raise ValueError(f"Unknown guild aid category: {row['ID']}")
        values = {key: int(row[field]) for key, field in
                  (("max", "MaxCount"), ("cost", "PerSupportCount"), ("reward", "RewardCount"))}
        if any(value <= 0 for value in values.values()) or values["max"] % values["cost"]:
            raise ValueError(f"Invalid guild aid quantities: {row['ID']}")
        rules[row["ID"]] = {"group": GROUPS[row["ID"]], **values}
    if not rules:
        raise ValueError("Empty guild aid rules")
    loc = json.loads((source / "文本/本地化/loc_CHS_FINAL.json").read_text(encoding="utf-8-sig"))
    items = {}
    for row in table("Item", ("ID", "Name", "Tags")):
        tags = set(row["Tags"].split("~")) & rules.keys()
        if not tags:
            continue
        if len(tags) != 1:
            raise ValueError(f"Ambiguous guild aid tags: {row['ID']}")
        name = loc.get(row["Name"])
        if not isinstance(name, str) or not name.strip() or "\ufffd" in name:
            raise ValueError(f"Missing guild aid item name: {row['ID']}")
        items[row["ID"]] = {"name": name, "rule": next(iter(tags))}
    if not items:
        raise ValueError("Empty guild aid item catalog")
    return {"schema": 1, "source": {name: hashlib.sha256(
                (source / "文本/数据表" / f"sd_{name}.txt").read_bytes()).hexdigest()
            for name in ("GuildAidItem", "Item")}, "rules": rules, "items": dict(sorted(items.items()))}


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
    print(f"Guild aid catalog: {len(data['items'])} items -> {args.target}")
