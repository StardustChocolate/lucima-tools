"""Generate the runtime commodity limit catalog from the unpacked game table."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "assets_full" / "文本" / "数据表" / "sd_Commodity.txt"
DEFAULT_TARGET = ROOT / "backend" / "commodity_catalog.json"
RESET_TYPES = {"Day", "Week", "Month", "Hour"}


def _limit_count(value: str, commodity_id: str) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        count = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"invalid LimitCount for commodity {commodity_id}: {raw!r}"
        ) from exc
    return count if count >= 0 else None


def read_catalog(source: Path) -> dict[str, dict]:
    if not source.is_file():
        raise RuntimeError(f"missing commodity table: {source}")
    try:
        with source.open(encoding="utf-8-sig", newline="") as stream:
            rows = csv.DictReader(stream, delimiter="@")
            required = {"ID", "Store", "ResetCountType", "LimitCount"}
            if not rows.fieldnames or not required.issubset(rows.fieldnames):
                raise RuntimeError(f"invalid commodity table header: {source}")
            catalog: dict[str, dict] = {}
            for row in rows:
                commodity_id = (row.get("ID") or "").strip()
                if not commodity_id:
                    continue
                reset_type = (row.get("ResetCountType") or "").strip()
                if reset_type and reset_type not in RESET_TYPES:
                    raise RuntimeError(
                        f"invalid ResetCountType for commodity {commodity_id}: {reset_type!r}"
                    )
                entry = {
                    "store": (row.get("Store") or "").strip(),
                    "resetType": reset_type or None,
                    "limit": _limit_count(row.get("LimitCount") or "", commodity_id),
                }
                previous = catalog.get(commodity_id)
                if previous is not None and previous != entry:
                    raise RuntimeError(f"conflicting duplicate commodity ID: {commodity_id}")
                catalog[commodity_id] = entry
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RuntimeError(f"invalid commodity table {source}: {exc}") from exc
    if not catalog:
        raise RuntimeError(f"commodity table is empty: {source}")
    return dict(sorted(catalog.items()))


def write_catalog(target: Path, catalog: dict[str, dict]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "source": "sd_Commodity.txt",
        "commodities": catalog,
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def sync(source: Path = DEFAULT_SOURCE, target: Path = DEFAULT_TARGET) -> int:
    catalog = read_catalog(source.resolve())
    write_catalog(target.resolve(), catalog)
    return len(catalog)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    try:
        count = sync(args.source, args.target)
    except RuntimeError as exc:
        parser.error(str(exc))
    print(f"Generated commodity catalog: {count} entries -> {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
