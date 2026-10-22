import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.update_security import verify_manifest_envelope
from tools import analyze_release_delta, package_release, publish_update


class ReleaseDistributionTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix="lucima-release-"))
        self.root = Path(self.directory)

    def test_manifest_reuses_exact_archive_and_retains_minimum_version(self):
        version = publish_update.current_version()
        release = self.root / version
        release.mkdir()
        archive_path = release / f"LucimaTools-windows-x64-{version}.zip"
        endpoint = "https://download.example/releases/stable.json"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("LucimaTools.exe", b"release executable")
            archive.writestr("_internal/update-endpoint.json", json.dumps({"manifestUrl": endpoint}))
        original = archive_path.read_bytes()
        (self.root / "CHANGELOG.md").write_text(
            f"## [{version}]\n\n- Verified downloads\n- Automatic relogin\n\n## [1.0.0]\n- Old notes\n",
            encoding="utf-8",
        )
        (self.root / "backend").mkdir()
        (self.root / "backend/version.py").write_text(f'APP_VERSION = "{version}"\n')
        manifest = self.root / "preview.json"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = key.public_key().public_numbers()
        public = {
            "algorithm": "RS256", "keyId": "test-key",
            "modulus": publish_update.b64url(numbers.n.to_bytes(256, "big")),
            "exponent": publish_update.b64url(numbers.e.to_bytes(3, "big")),
        }
        private_path = self.root / "test-private.pem"
        public_path = self.root / "test-public.json"
        private_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        public_path.write_text(json.dumps(public))
        arguments = [
            "publish_update", "--release-dir", str(release), "--update-url", endpoint,
            "--base-url", "https://download.example", "--manifest", str(manifest),
            "--output-dir", str(self.root),
            "--private-key", str(private_path), "--public-key", str(public_path),
        ]
        with patch.object(publish_update, "ROOT", self.root), \
                patch.object(sys, "argv", arguments), patch.dict(os.environ, {"LUCIMA_MIN_SUPPORTED": ""}), \
                patch.object(publish_update, "existing_manifest", return_value={"minSupportedVersion": "1.2.1"}):
            self.assertEqual(publish_update.main(), 0)
        payload = verify_manifest_envelope(manifest.read_bytes(), public_key=public)
        artifact = payload["platforms"]["windows"]
        self.assertEqual(artifact["url"], f"https://download.example/releases/{version}/{archive_path.name}")
        self.assertEqual(artifact["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(artifact["size"], len(original))
        self.assertEqual(archive_path.read_bytes(), original)
        self.assertEqual(payload["notes"], ["Verified downloads", "Automatic relogin"])
        self.assertEqual(payload["minSupportedVersion"], "1.2.1")
        self.assertEqual(payload["channel"], "preview")
        self.assertFalse((self.root / "stable.json").exists())
        self.assertIsNone(payload["platforms"]["android"])

    def test_release_notes_do_not_reuse_another_version(self):
        (self.root / "CHANGELOG.md").write_text("## [1.2.5]\n- Existing notes\n", encoding="utf-8")
        with patch.object(publish_update, "ROOT", self.root):
            self.assertEqual(publish_update.release_notes("1.2.6"), [])
            self.assertEqual(publish_update.release_notes("1.2.5"), ["Existing notes"])

    def test_packaging_requires_independent_service_endpoints(self):
        source = self.root / "bundle"
        (source / "_internal").mkdir(parents=True)
        (source / "LucimaTools.exe").write_bytes(b"release")
        endpoint = "https://download.example/releases/stable.json"
        (source / "_internal/update-endpoint.json").write_text(json.dumps({"manifestUrl": endpoint}))
        service = self.root / "redeem-codes-endpoint.json"
        service.write_text(json.dumps({"apiUrl": "https://service.example/api/redeem-codes"}))
        with patch.object(package_release, "ROOT", self.root):
            with self.assertRaisesRegex(SystemExit, "service endpoint"):
                package_release.validate_windows(source, endpoint)
            (source / "_internal" / service.name).write_bytes(service.read_bytes())
            package_release.validate_windows(source, endpoint)

    def test_delta_comparison_detects_changes_and_removals(self):
        old = self.root / "previous.zip"
        new = self.root / "current.zip"
        with zipfile.ZipFile(old, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("same", b"stable runtime" * 1000)
            archive.writestr("changed", b"old")
            archive.writestr("removed", b"gone")
        with zipfile.ZipFile(new, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("same", b"stable runtime" * 1000)
            archive.writestr("changed", b"new")
            archive.writestr("added", b"new file")
        result = analyze_release_delta.compare(old, new)
        self.assertEqual(result["unchangedFiles"], 1)
        self.assertEqual(result["changedFiles"], 1)
        self.assertEqual(result["addedFiles"], 1)
        self.assertEqual(result["removedEntries"], ["removed"])
        self.assertLess(result["changedEntryCompressedBytes"], result["fullDownloadBytes"])


if __name__ == "__main__":
    unittest.main()
