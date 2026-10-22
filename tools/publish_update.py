"""Build, sign, and optionally publish a LucimaTools update manifest."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError as exc:  # pragma: no cover - release workstation setup
    raise SystemExit("Install release dependencies: pip install -r requirements-release.txt") from exc


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.update_security import verify_manifest_envelope  # noqa: E402
from tools.package_release import expected_endpoint, validate_android  # noqa: E402
from tools.package_release import write_checksums  # noqa: E402
from tools.build_update_delta import build_android, build_windows  # noqa: E402
from backend.updater import _validate_artifact, _version_key  # noqa: E402


DEFAULT_PRIVATE_KEY = Path.home() / ".lucimatools" / "update-signing" / "private.pem"
DEFAULT_PUBLIC_KEY = ROOT / "backend" / "update_public_key.json"
DEFAULT_OUTPUT = ROOT / "releases"
DEFAULT_MANIFEST = DEFAULT_OUTPUT / "preview.json"
DEFAULT_BASE_URL = os.environ.get("LUCIMA_UPDATE_BASE_URL", "").strip()
VERSION_RE = re.compile(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']')
SAFE_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")


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


def release_notes(version: str) -> list[str]:
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8-sig").splitlines()
    found = False
    notes = []
    for line in lines:
        if line.startswith("## "):
            if found:
                break
            found = line.startswith(f"## [{version}]")
        elif found and line.startswith("- "):
            notes.append(line[2:].strip())
    return notes


def existing_manifest(path: Path) -> dict:
    for candidate in (path, path.parent / "stable.json", ROOT / "deploy" / "update-server" / "stable.json"):
        if candidate.is_file():
            return verify_manifest_envelope(candidate.read_bytes())
    return {}


def write_manifest(path: Path, envelope: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(envelope, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_promotion(source: Path, destination: Path, output: Path,
                      private_key: Path, public_key: Path) -> tuple[dict, dict | None]:
    """Validate and sign a promotion without changing local release records."""
    if destination.name != "stable.json" or source.resolve() == destination.resolve():
        raise SystemExit("Promotion must write a separate stable.json")
    public = json.loads(public_key.read_text(encoding="utf-8"))
    payload = verify_manifest_envelope(source.read_bytes(), public_key=public)
    if payload.get("channel") != "preview" or payload.get("development"):
        raise SystemExit("Promotion requires a signed non-development preview manifest")
    version = str(payload.get("latestVersion") or "")
    _version_key(version)
    minimum = str(payload.get("minSupportedVersion") or "")
    if _version_key(minimum) > _version_key(version):
        raise SystemExit("Invalid minimum supported version")
    previous_envelope = None
    if destination.exists():
        previous_bytes = destination.read_bytes()
        previous = verify_manifest_envelope(previous_bytes, public_key=public)
        previous_envelope = json.loads(previous_bytes)
        if previous.get("channel") != "stable" or previous.get("development"):
            raise SystemExit("Existing stable manifest is not a production stable release")
        if _version_key(str(previous["latestVersion"])) > _version_key(version):
            raise SystemExit("Cannot promote an older release")
        if previous["latestVersion"] == version and previous["platforms"] != payload["platforms"]:
            raise SystemExit("Cannot replace immutable stable artifacts with different bytes")
    for platform in ("windows", "android"):
        item = _validate_artifact(payload["platforms"].get(platform), platform)
        expected_format = "android-apk" if platform == "android" else "windows-portable-zip"
        if item is None or item["format"] != expected_format:
            raise SystemExit("Promotion requires both platform packages")
        for descriptor in [item, *item["deltas"]]:
            path = output / version / descriptor["fileName"]
            if not path.is_file() or path.stat().st_size != descriptor["size"] \
                    or file_sha256(path) != descriptor["sha256"]:
                raise SystemExit(f"Promotion artifact missing or changed: {path.name}")
    payload["channel"] = "stable"
    payload["publishedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    envelope = sign_payload(payload, private_key, public_key)
    return envelope, previous_envelope


def save_promotion(destination: Path, output: Path, envelope: dict,
                   previous_envelope: dict | None, public_key: dict | None = None) -> None:
    """Record the exact envelope already prepared (or uploaded) by the caller."""
    version = verify_manifest_envelope(json.dumps(envelope), public_key=public_key)["latestVersion"]
    if previous_envelope is not None:
        previous_version = str(verify_manifest_envelope(json.dumps(previous_envelope), public_key=public_key)["latestVersion"])
        _version_key(previous_version)
        if previous_version != version:
            write_manifest(output / "stable-history" / f"{previous_version}.json", previous_envelope)
    write_manifest(destination, envelope)


def promote(source: Path, destination: Path, output: Path, private_key: Path, public_key: Path) -> None:
    envelope, previous_envelope = prepare_promotion(source, destination, output, private_key, public_key)
    public = json.loads(public_key.read_text(encoding="utf-8"))
    save_promotion(destination, output, envelope, previous_envelope, public)
    print(f"Promoted verified artifacts locally: {destination.resolve()}")
    print("Stable metadata is ready for distribution. Nothing has been uploaded.")


def default_delta_baselines(output: Path, version: str, public_key: dict | None = None) -> list[Path]:
    stable_versions = set()
    for path in [output / "stable.json", *sorted((output / "stable-history").glob("*.json"))]:
        if not path.is_file():
            continue
        payload = verify_manifest_envelope(path.read_bytes(), public_key=public_key)
        if payload.get("schema") != 1 or payload.get("channel") != "stable" or payload.get("development"):
            raise SystemExit(f"Invalid stable release record: {path}")
        previous = str(payload.get("latestVersion") or "")
        if _version_key(previous) < _version_key(version):
            stable_versions.add(previous)
    baselines = [output / previous for previous in sorted(stable_versions, key=_version_key, reverse=True)[:2]]
    # Keep the latest local candidate for preview-to-preview update tests too.
    available = [p for p in output.iterdir() if p.is_dir()
                 and re.fullmatch(r"\d+\.\d+\.\d+", p.name)
                 and _version_key(p.name) < _version_key(version)]
    baselines.extend(sorted(available, key=lambda p: _version_key(p.name), reverse=True)[:1])
    return list(dict.fromkeys(baselines))


def add_deltas(platforms: dict, files: list[Path], release_dir: Path, output: Path,
               version: str, base_url: str, baselines: list[Path] | None,
               public_key: dict | None = None) -> None:
    if baselines is None:
        baselines = default_delta_baselines(output, version, public_key)
    if len(baselines) > 8:
        raise SystemExit("At most 8 delta baselines are supported")
    for baseline in baselines:
        previous = baseline.name
        if _version_key(previous) >= _version_key(version):
            raise SystemExit("Delta baseline must be older than target")
        for platform, pattern, builder, kind in (
            ("windows", "LucimaTools-windows-x64-{}.zip", build_windows, "windows-file-delta-v1"),
            ("android", "LucimaTools-android-{}.apk", build_android, "android-archive-delta-v1"),
        ):
            current = platforms.get(platform)
            base = baseline / pattern.format(previous)
            if current is None or not base.is_file():
                continue
            target = next(p for p in files if p.name == current["fileName"])
            path = release_dir / f"LucimaTools-{platform}-{previous}-to-{version}.delta.zip"
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                extra = builder(base, target, temporary, version)
                if temporary.stat().st_size >= current["size"]:
                    print(f"Delta omitted (full package is smaller): {platform} {previous}")
                    continue
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            descriptor = artifact(path, base_url, version, kind)
            descriptor.update(extra, fromVersion=previous)
            current.setdefault("deltas", []).append(descriptor)
            files.append(path)
            print(f"Verified delta: {path.name} ({descriptor['size']} bytes)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=current_version())
    parser.add_argument("--min-supported", default=os.environ.get("LUCIMA_MIN_SUPPORTED"))
    parser.add_argument("--release-dir", type=Path, help="Use already validated ZIP/APK files without repackaging")
    parser.add_argument("--update-url", default=os.environ.get("LUCIMA_UPDATE_URL", ""))
    parser.add_argument("--windows-dir", type=Path)
    parser.add_argument("--android-apk", type=Path)
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--private-key", type=Path, default=DEFAULT_PRIVATE_KEY)
    parser.add_argument("--public-key", type=Path, default=DEFAULT_PUBLIC_KEY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--promote", type=Path, help="Promote a verified preview manifest locally; no rebuild or upload")
    parser.add_argument("--delta-from", type=Path, action="append", help="Baseline directory (default: last 2 stable releases plus latest local candidate)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--encyclopedia-resources", type=Path,
        help="JSON object merged into the signed manifest's optional resources field",
    )
    args = parser.parse_args()

    if args.promote:
        destination = args.manifest
        if destination == DEFAULT_MANIFEST:
            destination = args.output_dir / "stable.json"
        promote(args.promote.resolve(), destination.resolve(), args.output_dir.resolve(),
                args.private_key.expanduser().resolve(), args.public_key.resolve())
        return 0

    version = args.version.strip()
    if args.manifest.name == "stable.json":
        raise SystemExit("Candidate cannot overwrite stable.json; use preview.json and --promote after testing")
    stable_path = args.output_dir / "stable.json"
    if stable_path.is_file() and verify_manifest_envelope(stable_path.read_bytes()).get("latestVersion") == version:
        raise SystemExit("This version is already promoted to stable. Increment APP_VERSION before generating artifacts.")
    previous = existing_manifest(args.manifest)
    min_supported = str(args.min_supported or previous.get("minSupportedVersion") or "0.0.0").strip()
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
    parsed_base = urlsplit(args.base_url.strip())
    if parsed_base.scheme not in {"https", "http"} or not parsed_base.hostname \
            or parsed_base.username or parsed_base.password or parsed_base.query or parsed_base.fragment:
        raise SystemExit("Update base URL must be an HTTP(S) address without credentials, query, or fragment")
    if args.release_dir and (args.windows_dir or args.android_apk):
        raise SystemExit("Use --release-dir or platform inputs, not both")

    release_dir = args.output_dir.resolve() / version
    release_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    platforms: dict[str, dict | None] = {"windows": None, "android": None}

    if args.release_dir:
        if not args.update_url:
            raise SystemExit("--release-dir requires --update-url or LUCIMA_UPDATE_URL")
        for platform, filename, kind in (
            ("windows", f"LucimaTools-windows-x64-{version}.zip", "windows-portable-zip"),
            ("android", f"LucimaTools-android-{version}.apk", "android-apk"),
        ):
            path = args.release_dir.resolve() / filename
            if not path.is_file():
                continue
            if platform == "android":
                validate_android(path, args.update_url)
            else:
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                    if "LucimaTools.exe" not in names or any(
                        name != "LucimaTools.exe" and not name.startswith("_internal/") for name in names
                    ):
                        raise SystemExit("Invalid Windows release archive")
                    expected_endpoint(archive.read("_internal/update-endpoint.json"), args.update_url, "Windows release")
            platforms[platform] = artifact(path, args.base_url.strip(), version, kind)
            files.append(path)

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

    add_deltas(platforms, files, release_dir, args.output_dir.resolve(), version,
               args.base_url.strip(), args.delta_from,
               public_key=json.loads(args.public_key.read_text(encoding="utf-8")))

    resources = previous.get("resources")
    if args.encyclopedia_resources:
        resources_path = args.encyclopedia_resources.expanduser().resolve()
        if not resources_path.is_file():
            raise SystemExit(f"Encyclopedia resources file not found: {resources_path}")
        try:
            resources = json.loads(resources_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Invalid encyclopedia resources JSON: {resources_path}") from exc
        if not isinstance(resources, dict):
            raise SystemExit("Encyclopedia resources must be a JSON object")

    payload = {
        "schema": 1,
        "channel": "preview",
        "development": bool(args.development),
        "latestVersion": version,
        "minSupportedVersion": min_supported,
        "publishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": release_notes(version),
        "platforms": platforms,
    }
    if resources is not None:
        payload["resources"] = resources
    envelope = sign_payload(
        payload, args.private_key.expanduser().resolve(), args.public_key.resolve()
    )
    write_manifest(args.manifest, envelope)
    write_manifest(release_dir / "candidate.json", envelope)
    write_checksums(release_dir)
    print(f"Signed manifest: {args.manifest.resolve()}")
    for path in files:
        print(f"Artifact: {path} ({path.stat().st_size} bytes, sha256 {file_sha256(path)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
