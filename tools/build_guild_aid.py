"""Extract the small guild-aid catalog from the game's item tags and aid rules."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path("assets/guild_aid.json")
GROUPS = {"CatalystRare": "catalyst", "Rune1": "element", "Rune2": "element",
          "Mat1": "hunt", "Mat2": "hunt", "Mat3": "hunt", "Mat4": "hunt"}


def build(source: Path) -> dict:
    from backend.guild_data_build import build_guild_aid
    return build_guild_aid(source)


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
