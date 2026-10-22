"""Compare packaged releases without changing or generating installable patches."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def entries(archive: zipfile.ZipFile) -> dict:
    result = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        with archive.open(info) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        result[info.filename] = {
            "sha256": digest, "bytes": info.file_size,
            "compressedBytes": info.compress_size,
        }
    return result


def compare(previous: Path, current: Path) -> dict:
    with zipfile.ZipFile(previous) as archive:
        old = entries(archive)
    with zipfile.ZipFile(current) as archive:
        new = entries(archive)
    unchanged = [name for name in new if name in old and new[name]["sha256"] == old[name]["sha256"]]
    changed = [name for name in new if name in old and new[name]["sha256"] != old[name]["sha256"]]
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    transfer = sum(new[name]["compressedBytes"] for name in changed + added)
    return {
        "previous": previous.name,
        "current": current.name,
        "fullDownloadBytes": current.stat().st_size,
        "unchangedFiles": len(unchanged),
        "changedFiles": len(changed),
        "addedFiles": len(added),
        "removedFiles": len(removed),
        "reusableUncompressedBytes": sum(new[name]["bytes"] for name in unchanged),
        "changedEntryCompressedBytes": transfer,
        "estimatedFileDeltaSavingsPercent": round(100 * (1 - transfer / current.stat().st_size), 2),
        "largestChangedEntries": [
            {"name": name, **new[name]} for name in sorted(
                changed + added, key=lambda name: new[name]["compressedBytes"], reverse=True,
            )[:20]
        ],
        "removedEntries": removed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "schema": 1,
        "method": "Compare uncompressed ZIP entry SHA-256; sum new/changed entries' compressed sizes.",
        "limitations": [
            "Sizes estimate a file-based patch and exclude patch metadata and reconstruction overhead.",
            "This report is not an installable patch and does not enable incremental updates.",
            "Android patches must reconstruct the byte-identical signed APK, including ZIP metadata and signing blocks.",
            "Existing clients need a full update before they can apply a newly implemented patch format.",
        ],
        "platforms": {},
    }
    for platform, pattern in (("windows", "LucimaTools-windows-x64-*.zip"), ("android", "LucimaTools-android-*.apk")):
        old = list(args.previous.glob(pattern))
        new = list(args.current.glob(pattern))
        if len(old) == 1 and len(new) == 1:
            report["platforms"][platform] = compare(old[0], new[0])
    if not report["platforms"]:
        parser.error("No matching previous/current platform artifacts")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for platform, result in report["platforms"].items():
        print(f"{platform}: full={result['fullDownloadBytes']} bytes; "
              f"changed entries={result['changedEntryCompressedBytes']} bytes; "
              f"estimated savings={result['estimatedFileDeltaSavingsPercent']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
