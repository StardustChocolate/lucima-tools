"""Validate and package release artifacts by the application version."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.version import APP_VERSION  # noqa: E402
from backend.update_security import verify_manifest_envelope  # noqa: E402


DEFAULT_OUTPUT = ROOT / "releases"
ENDPOINT_PATH = "assets/web/update-endpoint.json"
SERVICE_ENDPOINTS = ("encyclopedia-endpoint.json", "redeem-codes-endpoint.json")
SAFE_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_java_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def expected_endpoint(raw: bytes, expected_url: str, label: str) -> None:
    try:
        endpoint = str(json.loads(raw.decode("utf-8")).get("manifestUrl") or "")
    except (UnicodeDecodeError, ValueError, AttributeError) as exc:
        raise SystemExit(f"{label} contains an invalid update endpoint") from exc
    if endpoint != expected_url:
        raise SystemExit(
            f"{label} update endpoint does not match LUCIMA_UPDATE_URL"
        )


def validate_windows(source: Path, expected_url: str) -> None:
    source = source.resolve()
    expected = {"LucimaTools.exe", "_internal"}
    if not source.is_dir() or {entry.name for entry in source.iterdir()} != expected:
        raise SystemExit(
            "Windows bundle must contain only LucimaTools.exe and _internal"
        )
    endpoint = source / "_internal" / "update-endpoint.json"
    if not endpoint.is_file():
        raise SystemExit("Windows release is missing _internal/update-endpoint.json")
    expected_endpoint(endpoint.read_bytes(), expected_url, "Windows release")
    for name in SERVICE_ENDPOINTS:
        local = ROOT / name
        if local.is_file():
            packaged = source / "_internal" / name
            if not packaged.is_file() or json.loads(packaged.read_bytes()) != json.loads(local.read_bytes()):
                raise SystemExit(f"Windows release is missing the configured service endpoint: {name}")


def android_sdk_root() -> Path:
    configured = (
        os.environ.get("ANDROID_HOME", "").strip()
        or os.environ.get("ANDROID_SDK_ROOT", "").strip()
    )
    if configured and Path(configured).is_dir():
        return Path(configured)
    local = ROOT / "android" / "local.properties"
    if local.is_file():
        for raw in local.read_text(encoding="utf-8").splitlines():
            if raw.startswith("sdk.dir="):
                value = raw.split("=", 1)[1].replace("\\:", ":").replace("\\\\", "\\")
                if Path(value).is_dir():
                    return Path(value)
    fallback = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"
    if fallback.is_dir():
        return fallback
    raise SystemExit("Android SDK was not found")


def build_tool(name: str) -> Path:
    roots = list((android_sdk_root() / "build-tools").glob("*"))
    roots.sort(
        key=lambda path: tuple(
            int(part) if part.isdigit() else 0 for part in path.name.split(".")
        ),
        reverse=True,
    )
    names = (f"{name}.bat", f"{name}.exe", name)
    for root in roots:
        for candidate_name in names:
            candidate = root / candidate_name
            if candidate.is_file():
                return candidate
    raise SystemExit(f"Android build tool was not found: {name}")


def run(command: list[str]) -> str:
    result = subprocess.run(
        command, check=False, text=True, capture_output=True, encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(detail or f"Command failed: {command[0]}")
    return result.stdout


def validate_android(apk: Path, expected_url: str) -> str:
    apk = apk.resolve()
    if not apk.is_file() or not zipfile.is_zipfile(apk):
        raise SystemExit(f"Android release APK is missing or invalid: {apk}")
    with zipfile.ZipFile(apk) as archive:
        if ENDPOINT_PATH not in archive.namelist():
            raise SystemExit(f"Android release is missing {ENDPOINT_PATH}")
        expected_endpoint(archive.read(ENDPOINT_PATH), expected_url, "Android release")
        for name in SERVICE_ENDPOINTS:
            local = ROOT / name
            packaged = f"assets/web/{name}"
            if local.is_file() and (
                packaged not in archive.namelist()
                or json.loads(archive.read(packaged)) != json.loads(local.read_bytes())
            ):
                raise SystemExit(f"Android release is missing the configured service endpoint: {name}")

    badging = run([str(build_tool("aapt")), "dump", "badging", str(apk)])
    version_match = re.search(r"versionName='([^']+)'", badging)
    if not version_match or version_match.group(1) != APP_VERSION:
        actual = version_match.group(1) if version_match else "unknown"
        raise SystemExit(
            f"Android versionName mismatch: expected {APP_VERSION}, got {actual}"
        )

    verification = run([
        str(build_tool("apksigner")), "verify", "--verbose", "--print-certs", str(apk),
    ])
    match = re.search(
        r"Signer #?\d* certificate SHA-256 digest:\s*([0-9a-fA-F]+)", verification,
    )
    if not match:
        match = re.search(r"certificate SHA-256 digest:\s*([0-9a-fA-F]+)", verification)
    if not match:
        raise SystemExit("Could not read the Android signing certificate fingerprint")
    actual_fingerprint = match.group(1).lower()

    properties_path = ROOT / "android" / "keystore.properties"
    if not properties_path.is_file():
        raise SystemExit("android/keystore.properties is missing")
    expected_fingerprint = re.sub(
        r"[^0-9a-f]", "",
        read_java_properties(properties_path).get("certificateSha256", "").lower(),
    )
    if len(expected_fingerprint) != 64:
        raise SystemExit("certificateSha256 is missing from android/keystore.properties")
    if actual_fingerprint != expected_fingerprint:
        raise SystemExit("Android signing certificate does not match keystore.properties")
    return actual_fingerprint


def archive_windows(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.resolve().rglob("*"), key=lambda item: item.as_posix().lower()):
            if path.is_file():
                archive.write(path, path.relative_to(source.resolve()).as_posix())


def write_checksums(directory: Path) -> None:
    artifacts = sorted(
        (path for path in directory.iterdir() if path.is_file() and path.name != "SHA256SUMS.txt"),
        key=lambda path: path.name.lower(),
    )
    contents = "".join(f"{file_sha256(path)}  {path.name}\n" for path in artifacts)
    (directory / "SHA256SUMS.txt").write_text(contents, encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--windows-dir", type=Path)
    parser.add_argument("--android-apk", type=Path)
    parser.add_argument("--update-url", required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    version = args.version.strip()
    update_url = args.update_url.strip()
    if version != APP_VERSION or not SAFE_VERSION_RE.fullmatch(version):
        raise SystemExit("Release version must match backend/version.py")
    if not update_url.startswith(("http://", "https://")):
        raise SystemExit("LUCIMA_UPDATE_URL must be an HTTP(S) URL")
    if not args.windows_dir and not args.android_apk:
        raise SystemExit("At least one platform artifact is required")

    if args.windows_dir:
        validate_windows(args.windows_dir, update_url)
    fingerprint = validate_android(args.android_apk, update_url) if args.android_apk else ""

    output_root = args.output_dir.resolve()
    stable = output_root / "stable.json"
    if stable.is_file() and verify_manifest_envelope(stable.read_bytes()).get("latestVersion") == version:
        raise SystemExit("This version is already promoted to stable. Increment APP_VERSION before rebuilding.")
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / version
    staging = output_root / f".{version}.{uuid.uuid4().hex}.tmp"
    staging.mkdir()
    try:
        if not args.clean and destination.is_dir():
            for existing in destination.iterdir():
                if existing.is_file() and existing.name != "SHA256SUMS.txt":
                    shutil.copy2(existing, staging / existing.name)
        if args.windows_dir:
            archive_windows(
                args.windows_dir,
                staging / f"LucimaTools-windows-x64-{version}.zip",
            )
        if args.android_apk:
            shutil.copy2(
                args.android_apk,
                staging / f"LucimaTools-android-{version}.apk",
            )
        write_checksums(staging)
        destination.mkdir(parents=True, exist_ok=True)
        staged_names = {path.name for path in staging.iterdir()}
        for existing in destination.iterdir():
            if args.clean or existing.name not in staged_names:
                if existing.is_dir():
                    shutil.rmtree(existing)
                else:
                    existing.unlink()
        for path in staging.iterdir():
            target = destination / path.name
            if target.exists():
                target.unlink()
            path.replace(target)
        staging.rmdir()
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(f"Verified release artifacts: {destination}")
    if fingerprint:
        print(f"Android certificate SHA-256: {fingerprint}")
    for path in sorted(destination.iterdir(), key=lambda item: item.name.lower()):
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
