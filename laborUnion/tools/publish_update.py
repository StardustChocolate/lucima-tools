"""Build, sign, and optionally publish a LucimaTools update manifest."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError as exc:  # pragma: no cover - release workstation setup
    raise SystemExit("Install release dependencies: pip install -r requirements-release.txt") from exc


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.update_security import verify_manifest_envelope  # noqa: E402


DEFAULT_PRIVATE_KEY = Path.home() / ".lucimatools" / "update-signing" / "private.pem"
DEFAULT_PUBLIC_KEY = ROOT / "backend" / "update_public_key.json"
DEFAULT_MANIFEST = ROOT / "deploy" / "update-server" / "stable.json"
DEFAULT_OUTPUT = ROOT / "build" / "update-release"
DEFAULT_BASE_URL = os.environ.get("LUCIMA_UPDATE_BASE_URL", "").strip()
VERSION_RE = re.compile(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']')
SAFE_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
SAFE_REMOTE_RE = re.compile(r"^/[0-9A-Za-z._/-]+$")


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def current_version() -> str:
    text = (ROOT / "backend" / "version.py").read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise SystemExit("Cannot read APP_VERSION from backend/version.py")
    return match.group(1)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_windows_archive(source: Path, destination: Path) -> None:
    source = source.resolve()
    expected = {"LucimaTools.exe", "_internal"}
    actual = {entry.name for entry in source.iterdir()}
    if actual != expected or not (source / "LucimaTools.exe").is_file() \
            or not (source / "_internal").is_dir():
        raise SystemExit(
            f"Windows bundle must contain only LucimaTools.exe and _internal: {source}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*"), key=lambda value: value.as_posix().lower()):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


def artifact(path: Path, base_url: str, version: str, kind: str) -> dict:
    return {
        "format": kind,
        "fileName": path.name,
        "url": f"{base_url.rstrip('/')}/releases/{version}/{path.name}",
        "size": path.stat().st_size,
        "sha256": file_sha256(path),
    }


def sign_payload(payload: dict, private_key_path: Path, public_key_path: Path) -> dict:
    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(), password=None
    )
    public_data = json.loads(public_key_path.read_text(encoding="utf-8"))
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    signature = private_key.sign(payload_bytes, padding.PKCS1v15(), hashes.SHA256())
    envelope = {
        "schema": 1,
        "algorithm": "RS256",
        "keyId": public_data["keyId"],
        "payload": b64url(payload_bytes),
        "signature": b64url(signature),
    }
    verify_manifest_envelope(
        json.dumps(envelope, separators=(",", ":")), public_key=public_data
    )
    return envelope


def run(command: list[str]) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def publish(server: str, remote_root: str, manifest_path: Path,
            artifacts: list[Path], version: str) -> None:
    if not SAFE_REMOTE_RE.fullmatch(remote_root):
        raise SystemExit(f"Unsafe remote root: {remote_root}")
    remote_release = f"{remote_root.rstrip('/')}/releases/{version}"
    run(["ssh", server, f"mkdir -p '{remote_release}'"])
    for path in artifacts:
        token = uuid.uuid4().hex
        temporary = f"{remote_release}/.{path.name}.{token}.upload"
        final = f"{remote_release}/{path.name}"
        run(["scp", str(path), f"{server}:{temporary}"])
        remote_hash = run(["ssh", server, f"sha256sum '{temporary}' | cut -d' ' -f1"])
        if remote_hash.lower() != file_sha256(path):
            raise SystemExit(f"Remote checksum mismatch: {path.name}")
        run(["ssh", server, f"mv -f '{temporary}' '{final}'"])

    token = uuid.uuid4().hex
    temporary_manifest = f"{remote_root.rstrip('/')}/.stable.{token}.upload"
    final_manifest = f"{remote_root.rstrip('/')}/stable.json"
    run(["scp", str(manifest_path), f"{server}:{temporary_manifest}"])
    run(["ssh", server, f"mv -f '{temporary_manifest}' '{final_manifest}'"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=current_version())
    parser.add_argument("--min-supported")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--windows-dir", type=Path)
    parser.add_argument("--android-apk", type=Path)
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--private-key", type=Path, default=DEFAULT_PRIVATE_KEY)
    parser.add_argument("--public-key", type=Path, default=DEFAULT_PUBLIC_KEY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--server", default=os.environ.get("LUCIMA_UPDATE_SERVER", ""))
    parser.add_argument("--remote-root", default=os.environ.get("LUCIMA_UPDATE_REMOTE_ROOT", ""))
    args = parser.parse_args()

    version = args.version.strip()
    min_supported = (args.min_supported or version).strip()
    if not SAFE_VERSION_RE.fullmatch(version) or not SAFE_VERSION_RE.fullmatch(min_supported):
        raise SystemExit("Version contains unsupported characters")
    if version != current_version():
        raise SystemExit("Published version must match backend/version.py")
    if not args.private_key.expanduser().is_file():
        raise SystemExit(f"Update signing key not found: {args.private_key.expanduser()}")
    if not args.public_key.is_file():
        raise SystemExit(f"Update public key not found: {args.public_key}")
    if not args.base_url.strip():
        raise SystemExit("Update base URL is required; pass --base-url or LUCIMA_UPDATE_BASE_URL")
    if args.publish and (not args.server.strip() or not args.remote_root.strip()):
        raise SystemExit("Publishing requires --server/--remote-root or their LUCIMA_UPDATE_* environment variables")

    release_dir = args.output_dir.resolve() / version
    release_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    platforms: dict[str, dict | None] = {"windows": None, "android": None}

    if args.windows_dir:
        windows_zip = release_dir / f"LucimaTools-windows-x64-{version}.zip"
        create_windows_archive(args.windows_dir, windows_zip)
        files.append(windows_zip)
        platforms["windows"] = artifact(
            windows_zip, args.base_url, version, "windows-portable-zip"
        )
    if args.android_apk:
        source_apk = args.android_apk.resolve()
        if not source_apk.is_file():
            raise SystemExit(f"Android APK not found: {source_apk}")
        android_apk = release_dir / f"LucimaTools-android-{version}.apk"
        shutil.copy2(source_apk, android_apk)
        files.append(android_apk)
        platforms["android"] = artifact(android_apk, args.base_url, version, "android-apk")
    if not files and not (args.allow_empty and args.development):
        raise SystemExit("No release artifacts supplied; use --allow-empty only for development")

    payload = {
        "schema": 1,
        "channel": "stable",
        "development": bool(args.development),
        "latestVersion": version,
        "minSupportedVersion": min_supported,
        "publishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": [str(note).strip() for note in args.note if str(note).strip()],
        "platforms": platforms,
    }
    envelope = sign_payload(
        payload, args.private_key.expanduser().resolve(), args.public_key.resolve()
    )
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(envelope, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Signed manifest: {args.manifest.resolve()}")
    for path in files:
        print(f"Artifact: {path} ({path.stat().st_size} bytes, sha256 {file_sha256(path)})")
    if args.publish:
        publish(args.server, args.remote_root, args.manifest.resolve(), files, version)
        print(f"Published {version} to {args.base_url.rstrip('/')}/stable.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
