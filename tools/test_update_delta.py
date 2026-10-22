import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend import config, updater
from backend.update_delta import (
    ANDROID_FORMAT, WINDOWS_FORMAT, DeltaError, apply_android, apply_windows, file_hash, windows_path,
)
from backend.update_security import verify_manifest_envelope
from tools import build_update_delta, publish_update


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="lucima-update-test-")))
        self.enterContext(patch.object(config, "get_update_channel", return_value="stable"))
        self.enterContext(patch.object(config, "get_ignored_update_version", return_value=""))
        self.enterContext(patch.object(config, "data_dir", return_value=self.root))
        self.old = self.root / "old.zip"
        self.new = self.root / "new.zip"
        same = os.urandom(100000)
        for path, contents in ((self.old, {"_internal/removed": b"old", "LucimaTools.exe": b"old"}),
                               (self.new, {"_internal/added": b"new", "LucimaTools.exe": b"new"})):
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("_internal/same", same)
                for name, data in contents.items():
                    archive.writestr(name, data)
                archive.comment = path.name.encode()
        self.patch = self.root / "update.delta.zip"
        self.artifact = {
            "fileName": "new.apk", "url": "https://download.example/new.apk",
            "size": self.new.stat().st_size, "sha256": file_hash(self.new), "format": "android-apk",
        }

    def test_windows_reuses_verifies_removes_and_adds(self):
        build_update_delta.build_windows(self.old, self.new, self.patch, "1.2.7")
        with zipfile.ZipFile(self.old) as archive:
            archive.extractall(self.root / "installed")
        apply_windows(self.patch, self.root / "installed", self.root / "stage", "1.2.7")
        with zipfile.ZipFile(self.new) as archive:
            for entry in archive.infolist():
                self.assertEqual((self.root / "stage" / entry.filename).read_bytes(), archive.read(entry))
        self.assertFalse((self.root / "stage/_internal/removed").exists())
        self.assertLess(self.patch.stat().st_size, self.new.stat().st_size / 10)

    def test_windows_modified_base_is_rejected(self):
        build_update_delta.build_windows(self.old, self.new, self.patch, "1.2.7")
        with zipfile.ZipFile(self.old) as archive:
            archive.extractall(self.root / "installed")
        (self.root / "installed/_internal/same").write_bytes(b"x" * 100000)
        with self.assertRaisesRegex(DeltaError, "hash mismatch"):
            apply_windows(self.patch, self.root / "installed", self.root / "stage", "1.2.7")

    def test_windows_rejects_unsafe_paths(self):
        for name in ("../outside", "_internal/../outside", "_internal/C:secret", "_internal/x\\y",
                     "_internal/aux.png", "_internal/file.", "_internal/file ", "/LucimaTools.exe",
                     "_internal//x", "_internal/x\x00", "settings.json"):
            with self.subTest(name=name), self.assertRaises(DeltaError):
                windows_path(name)

    def test_windows_rejects_duplicate_case_aliases(self):
        data = b"test"
        files = [{"path": n, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "changed": True}
                 for n in ("_internal/file", "_internal/FILE")]
        with zipfile.ZipFile(self.patch, "w") as archive:
            archive.writestr("recipe.json", json.dumps({"format": WINDOWS_FORMAT, "toVersion": "1.2.7", "files": files}))
            archive.writestr("files/_internal/file", data)
            archive.writestr("files/_internal/FILE", data)
        with self.assertRaisesRegex(DeltaError, "Duplicate"):
            apply_windows(self.patch, self.root, self.root / "stage", "1.2.7")

    def test_android_restores_archive_byte_for_byte(self):
        descriptor = build_update_delta.build_android(self.old, self.new, self.patch, "1.2.7")
        restored = self.root / "restored.apk"
        apply_android(self.patch, self.old, restored, self.artifact, descriptor)
        self.assertEqual(restored.read_bytes(), self.new.read_bytes())
        self.assertLess(self.patch.stat().st_size, self.new.stat().st_size / 10)

    def test_android_wrong_baseline_and_wrong_target_are_rejected(self):
        descriptor = build_update_delta.build_android(self.old, self.new, self.patch, "1.2.7")
        with self.assertRaisesRegex(DeltaError, "baseline"):
            apply_android(self.patch, self.new, self.root / "restored.apk", self.artifact, descriptor)
        with self.assertRaisesRegex(DeltaError, "verification"):
            apply_android(self.patch, self.old, self.root / "restored.apk",
                          {**self.artifact, "sha256": "0" * 64}, descriptor)

    def test_android_rejects_copy_outside_baseline(self):
        with zipfile.ZipFile(self.patch, "w") as archive:
            archive.writestr("recipe.json", json.dumps({"format": ANDROID_FORMAT,
                "operations": [["copy", self.old.stat().st_size, 10]]}))
            archive.writestr("data", b"")
        with self.assertRaisesRegex(DeltaError, "outside baseline"):
            apply_android(self.patch, self.old, self.root / "restored.apk", self.artifact,
                          {"baseSize": self.old.stat().st_size, "baseSha256": file_hash(self.old)})

    def test_android_runtime_uses_patch_without_full_download(self):
        descriptor = build_update_delta.build_android(self.old, self.new, self.patch, "1.2.7")
        descriptor.update(fromVersion=config.APP_VERSION, size=self.patch.stat().st_size)
        service = updater.UpdateService()
        with patch.object(service, "_download", return_value=self.patch) as download, \
                patch.dict(os.environ, {"ARK_ANDROID_APK_PATH": str(self.old)}):
            path, stage = service._prepare_update({**self.artifact, "deltas": [descriptor]}, "1.2.7", "android")
        self.assertFalse(stage)
        self.assertEqual(path.read_bytes(), self.new.read_bytes())
        download.assert_called_once_with(descriptor, "1.2.7")
        self.assertFalse(self.patch.exists())

    def test_bad_patch_falls_back_once_and_cleans_temporary_files(self):
        self.patch.write_bytes(b"corrupted patch")
        descriptor = {"fromVersion": config.APP_VERSION, "format": ANDROID_FORMAT, "size": 15,
                      "baseSize": self.old.stat().st_size, "baseSha256": file_hash(self.old)}
        service = updater.UpdateService()
        artifact = {**self.artifact, "deltas": [descriptor]}
        with patch.object(service, "_download", side_effect=[self.patch, self.new]) as download, \
                patch.dict(os.environ, {"ARK_ANDROID_APK_PATH": str(self.old)}):
            path, stage = service._prepare_update(artifact, "1.2.7", "android")
        self.assertEqual(path, self.new)
        self.assertFalse(stage)
        self.assertEqual(download.call_args_list[0].args[0], descriptor)
        self.assertEqual(download.call_args_list[1].args[0], artifact)
        self.assertEqual(download.call_count, 2)
        self.assertFalse(self.patch.exists())
        self.assertFalse(list(self.root.glob("*.part")))

    def test_no_baseline_does_not_download_patch(self):
        service = updater.UpdateService()
        artifact = {**self.artifact, "deltas": [{"fromVersion": "0.0.1", "format": ANDROID_FORMAT, "size": 1}]}
        with patch.object(service, "_download", return_value=self.new) as download:
            service._prepare_update(artifact, "1.2.7", "android")
        download.assert_called_once_with(artifact, "1.2.7")

    def test_windows_runtime_uses_patch(self):
        build_update_delta.build_windows(self.old, self.new, self.patch, "1.2.7")
        with zipfile.ZipFile(self.old) as archive:
            archive.extractall(self.root / "installed")
        descriptor = {"fromVersion": config.APP_VERSION, "format": WINDOWS_FORMAT, "size": self.patch.stat().st_size}
        service = updater.UpdateService()
        artifact = {**self.artifact, "format": "windows-portable-zip", "deltas": [descriptor]}
        with patch.object(sys, "executable", str(self.root / "installed/LucimaTools.exe")), \
                patch.object(service, "_download", return_value=self.patch):
            path, stage = service._prepare_update(artifact, "1.2.7", "windows")
        self.assertTrue(stage)
        self.assertEqual((path / "LucimaTools.exe").read_bytes(), b"new")

    def test_channels_reset_cache_and_only_read_selected_manifest(self):
        service = updater.UpdateService()
        service._etag = "old-etag"
        service._payload = {"latestVersion": "1.2.7"}
        with patch.object(config, "TEST_SERVER", False), patch.object(config, "set_update_channel") as save, \
                patch.object(service, "check_now", return_value=True), \
                patch.object(updater, "MANIFEST_URL", "https://download.example/releases/stable.json"):
            state = service.set_channel("preview")
            self.assertEqual(service.manifest_url(), "https://download.example/releases/preview.json")
            self.assertIsNone(service._payload)
            self.assertEqual(service._etag, "")
            self.assertEqual(state["channel"], "preview")
            save.assert_called_once_with("preview")
        payload = {"schema": 1, "channel": "stable", "latestVersion": "1.2.7",
                   "minSupportedVersion": "1.2.1", "platforms": {}}
        with self.assertRaises(updater.UpdateError):
            service._validated_payload(payload)
        service._validated_payload({**payload, "channel": "preview"})

    def test_cannot_change_channel_during_install(self):
        service = updater.UpdateService()
        with service._install_lock, self.assertRaises(updater.UpdateError):
            service.set_channel("preview")

    def test_test_server_does_not_request_updates_on_either_channel(self):
        service = updater.UpdateService()
        service._channel = "preview"
        with patch.object(config, "TEST_SERVER", True), patch.object(updater, "_http_client") as client:
            service.start()
            self.assertTrue(service.check_now())
        client.assert_not_called()

    def test_download_hash_mismatch_removes_partial_file(self):
        service = updater.UpdateService()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"wrong"))
        with patch.object(updater, "_http_client", return_value=httpx.Client(transport=transport)):
            with self.assertRaises(updater.UpdateError):
                service._download({**self.artifact, "size": 5}, "1.2.7")
        self.assertFalse(list((self.root / "updates/downloads").iterdir()))

    def test_api_channel_requires_developer_auth(self):
        from backend.server import _route_api
        from backend.appcore import ApiError
        with patch.object(config, "check_dev_pass", return_value=False):
            with self.assertRaises(ApiError):
                _route_api("POST", "/api/update/channel", {"channel": "preview"}, {})


class PromotionTests(unittest.TestCase):
    def test_promotion_retains_exact_artifacts_and_rejects_changed_package(self):
        with tempfile.TemporaryDirectory(prefix="lucima-promotion-") as directory:
            root = Path(directory)
            release = root / "1.2.7"
            release.mkdir()
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            numbers = key.public_key().public_numbers()
            public = {"algorithm": "RS256", "keyId": "test",
                      "modulus": publish_update.b64url(numbers.n.to_bytes(256, "big")),
                      "exponent": publish_update.b64url(numbers.e.to_bytes(3, "big"))}
            public_path = root / "public.json"
            private_path = root / "private.pem"
            public_path.write_text(json.dumps(public))
            private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            platforms = {}
            for name, fmt in (("windows", "windows-portable-zip"), ("android", "android-apk")):
                path = release / (name + ".zip")
                path.write_bytes(os.urandom(20))
                platforms[name] = publish_update.artifact(path, "https://download.example", "1.2.7", fmt)
            payload = {"schema": 1, "channel": "preview", "latestVersion": "1.2.7",
                       "minSupportedVersion": "1.2.1", "platforms": platforms, "notes": ["Tested candidate"]}
            preview = root / "preview.json"
            stable = root / "stable.json"
            previous_payload = {**payload, "channel": "stable", "latestVersion": "1.2.5"}
            publish_update.write_manifest(stable, publish_update.sign_payload(previous_payload, private_path, public_path))
            previous_bytes = stable.read_bytes()
            publish_update.write_manifest(preview, publish_update.sign_payload(payload, private_path, public_path))
            preview_bytes = preview.read_bytes()
            publish_update.promote(preview, stable, root, private_path, public_path)
            result = verify_manifest_envelope(stable.read_bytes(), public_key=public)
            self.assertEqual(result["channel"], "stable")
            self.assertEqual(result["platforms"], payload["platforms"])
            self.assertEqual(result["notes"], payload["notes"])
            self.assertEqual(preview.read_bytes(), preview_bytes)
            history = root / "stable-history" / "1.2.5.json"
            self.assertEqual(json.loads(history.read_bytes()), json.loads(previous_bytes))
            archived = verify_manifest_envelope(history.read_bytes(), public_key=public)
            self.assertEqual(archived["channel"], "stable")
            self.assertEqual(archived["latestVersion"], "1.2.5")
            stable_bytes = stable.read_bytes()
            (release / "android.zip").write_bytes(b"changed")
            with self.assertRaisesRegex(SystemExit, "missing or changed"):
                publish_update.promote(preview, stable, root, private_path, public_path)
            self.assertEqual(stable.read_bytes(), stable_bytes)
            with self.assertRaisesRegex(SystemExit, "separate stable.json"):
                publish_update.promote(preview, preview, root, private_path, public_path)
            self.assertEqual(preview.read_bytes(), preview_bytes)


class BaselinePolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="lucima-baselines-")))
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = key.public_key().public_numbers()
        self.public = {"algorithm": "RS256", "keyId": "test-baselines",
                       "modulus": publish_update.b64url(numbers.n.to_bytes(256, "big")),
                       "exponent": publish_update.b64url(numbers.e.to_bytes(3, "big"))}
        self.public_path = self.root / "public.json"
        self.private_path = self.root / "private.pem"
        self.public_path.write_text(json.dumps(self.public))
        self.private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    def record(self, path, version, channel="stable"):
        payload = {"schema": 1, "channel": channel, "latestVersion": version}
        publish_update.write_manifest(path, publish_update.sign_payload(payload, self.private_path, self.public_path))

    def test_last_two_stable_releases_and_latest_candidate(self):
        for version in ("1.2.5", "1.2.6", "1.2.7", "1.2.8", "1.2.9", "1.3.0"):
            (self.root / version).mkdir()
        self.record(self.root / "stable.json", "1.2.8")
        self.record(self.root / "stable-history/1.2.5.json", "1.2.5")
        self.record(self.root / "stable-history/1.2.6.json", "1.2.6")
        before = (self.root / "stable.json").read_bytes()
        baselines = publish_update.default_delta_baselines(self.root, "1.3.0", self.public)
        self.assertEqual([path.name for path in baselines], ["1.2.8", "1.2.6", "1.2.9"])
        self.assertEqual((self.root / "stable.json").read_bytes(), before)

    def test_deduplicates_latest_local_release_and_ignores_target_or_newer(self):
        for version in ("1.2.8", "1.2.9", "1.3.0"):
            (self.root / version).mkdir()
        self.record(self.root / "stable.json", "1.2.8")
        self.record(self.root / "stable-history/1.2.9.json", "1.2.9")
        baselines = publish_update.default_delta_baselines(self.root, "1.2.9", self.public)
        self.assertEqual([path.name for path in baselines], ["1.2.8"])

    def test_no_stable_history_keeps_preview_test_baseline(self):
        (self.root / "1.2.6").mkdir()
        self.assertEqual(publish_update.default_delta_baselines(self.root, "1.2.7", self.public),
                         [self.root / "1.2.6"])

    def test_invalid_stable_history_is_rejected(self):
        self.record(self.root / "stable-history/1.2.6.json", "1.2.6", "preview")
        with self.assertRaisesRegex(SystemExit, "Invalid stable release record"):
            publish_update.default_delta_baselines(self.root, "1.3.0", self.public)


if __name__ == "__main__":
    unittest.main()
