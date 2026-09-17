"""Package locally built Windows and Android debug artifacts by app version."""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.version import APP_VERSION  # noqa: E402


SAFE_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
DEFAULT_OUTPUT = ROOT / "debug"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_windows(source: Path, destination: Path) -> None:
    source = source.resolve()
    expected = {"LucimaTools.exe", "_internal"}
    if not source.is_dir() or {entry.name for entry in source.iterdir()} != expected \
            or not (source / "LucimaTools.exe").is_file() \
            or not (source / "_internal").is_dir():
        raise SystemExit(
            "Windows bundle must contain only LucimaTools.exe and _internal"
        )

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(source.rglob("*"), key=lambda value: value.as_posix().lower()):
                if path.is_file():
                    archive.write(path, path.relative_to(source).as_posix())
        if not zipfile.is_zipfile(temporary):
            raise SystemExit("Generated Windows package is not a valid ZIP archive")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def package_android(source: Path, destination: Path) -> None:
    source = source.resolve()
    if not source.is_file() or not zipfile.is_zipfile(source):
        raise SystemExit(f"Android debug APK is missing or invalid: {source}")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--windows-dir", type=Path)
    parser.add_argument("--android-apk", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    version = args.version.strip()
    if not SAFE_VERSION_RE.fullmatch(version):
        raise SystemExit("Version contains unsupported characters")
    if not args.windows_dir and not args.android_apk:
        raise SystemExit("At least one platform artifact is required")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    version_dir = output_dir / version
    staging_dir = output_dir / f".{version}.{uuid.uuid4().hex}.tmp" if args.clean else None
    package_dir = staging_dir or version_dir
    package_dir.mkdir(parents=True, exist_ok=False if staging_dir else True)

    packaged: list[Path] = []
    try:
        if args.windows_dir:
            destination = package_dir / f"LucimaTools-windows-x64-debug-{version}.zip"
            package_windows(args.windows_dir, destination)
            packaged.append(destination)
        if args.android_apk:
            destination = package_dir / f"LucimaTools-android-debug-{version}.apk"
            package_android(args.android_apk, destination)
            packaged.append(destination)
        if staging_dir:
            version_dir.mkdir(parents=True, exist_ok=True)
            expected_names = {path.name for path in packaged}
            for old_path in version_dir.iterdir():
                if old_path.name not in expected_names:
                    if old_path.is_dir():
                        shutil.rmtree(old_path)
                    else:
                        old_path.unlink()
            for path in packaged:
                path.replace(version_dir / path.name)
            shutil.rmtree(staging_dir)
            packaged = [version_dir / path.name for path in packaged]
    except BaseException:
        if staging_dir:
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    for path in packaged:
        print(f"Debug artifact: {path} ({path.stat().st_size} bytes)")
        print(f"SHA-256: {file_sha256(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
