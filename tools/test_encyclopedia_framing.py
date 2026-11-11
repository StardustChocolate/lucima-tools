"""Offline camera contracts: transparent texels, additive light and source binding."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright
from stage_encyclopedia_resources import FRAMING_RULE, resource_sources
from stage_encyclopedia_cdn import stage_cdn
from build_encyclopedia import build_bundle
from build_cloud_encyclopedia import build
from encyclopedia_release import collect_release
import test_encyclopedia_catalog as fixtures

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "frontend/encyclopedia-framing.js"


class FramingPublicationTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.EncyclopediaCatalogTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.source = fixture.root, fixture.source
        self.sidecar = self.source / "立绘/H001.framing.json"
        helper = self.root / "out/assets/spine/portrait-framing.js"
        helper.write_bytes(HELPER.read_bytes())
        directory = self.source / "动态立绘/H001"
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        self.profile = {
            "schema": 1, "rule": FRAMING_RULE, "base": "H001",
            "skin": "LV1", "animation": "00_Idle",
            "bounds": {"centerX": 0, "centerY": 0, "width": 100, "height": 100},
            "static_sha256": sha(self.source / "立绘/H001.png"),
            "helper_sha256": sha(helper),
            "runtime_sha256": sha(helper.with_name("spine-webgl.js")),
            "sources": {path.name: {"bytes": path.stat().st_size, "sha256": sha(path)}
                        for path in directory.iterdir()},
        }
        self.save()

    def save(self):
        self.sidecar.write_text(json.dumps(self.profile), encoding="utf-8")

    def test_valid_pair_is_published_and_legacy_without_sidecar_still_works(self):
        sources, counts = resource_sources(self.root)
        self.assertEqual(sources["portraits/spine/H001/H001.framing.json"], self.sidecar)
        self.assertEqual(counts["spine_files"], 4)
        self.sidecar.unlink()
        self.assertEqual(resource_sources(self.root)[1]["spine_files"], 3)

    def test_new_camera_release_preserves_legacy_entry_and_requires_new_renderer(self):
        pool = self.root / "pool"
        saved = self.sidecar.read_bytes()
        self.sidecar.unlink()
        stage_cdn(self.root, pool, "legacy")
        old_entry = (pool / "manifest.json").read_bytes()
        self.sidecar.write_bytes(saved)
        report = stage_cdn(self.root, pool, "new-camera")
        self.assertTrue(report["computedCameras"])
        self.assertEqual((pool / "manifest.json").read_bytes(), old_entry)
        bundle = self.root / "bundle/encyclopedia"
        build_bundle(self.root, bundle)
        avatars = self.root / "avatars"
        avatars.mkdir()
        build(bundle, pool, avatars, "new-camera")
        entry = json.loads((pool / "latest.json").read_bytes())
        self.assertEqual(entry["renderer"], 2)
        self.assertEqual(entry["media"]["path"], "manifests/new-camera.json")
        release = collect_release(pool)
        self.assertEqual([item.path for item in release.entries], ["latest.json"])
        self.assertIn("manifests/new-camera.json", [item.path for item in release.files])

    def test_changed_texture_png_runtime_or_rule_cannot_publish_stale_camera(self):
        paths = [self.source / "动态立绘/H001/CG_H001_a.png", self.source / "立绘/H001.png",
                 self.root / "out/assets/spine/spine-webgl.js",
                 self.root / "out/assets/spine/portrait-framing.js"]
        for path in paths:
            with self.subTest(path=path.name):
                old = path.read_bytes()
                path.write_bytes(old + b"changed")
                with self.assertRaises(ValueError):
                    resource_sources(self.root)
                path.write_bytes(old)
        self.profile["rule"] = "obsolete"
        self.save()
        with self.assertRaises(ValueError):
            resource_sources(self.root)

    def test_nonfinite_camera_and_foreign_source_are_rejected(self):
        self.profile["bounds"]["width"] = float("nan")
        self.save()
        with self.assertRaises(ValueError):
            resource_sources(self.root)
        self.profile["bounds"]["width"] = 100
        self.profile["sources"]["../H002.png"] = {}
        self.save()
        with self.assertRaises(ValueError):
            resource_sources(self.root)

    def test_new_render_profile_requires_its_exact_camera_sidecar(self):
        path = self.source / "立绘/_render_profile.json"
        path.write_text(json.dumps({"bounds": FRAMING_RULE, "portraits": {"H001": {
            "framing_sha256": hashlib.sha256(self.sidecar.read_bytes()).hexdigest()}}}), encoding="utf-8")
        resource_sources(self.root)
        self.sidecar.unlink()
        with self.assertRaises(ValueError):
            resource_sources(self.root)
        self.profile["bounds"]["width"] = 99
        self.save()
        with self.assertRaises(ValueError):
            resource_sources(self.root)


class FramingPixelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.page = cls.browser.new_page()
        cls.page.route("**/*", lambda route: route.fulfill(status=200, body="<!doctype html>"))
        cls.page.goto("https://framing.test")
        cls.page.add_script_tag(content=HELPER.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_transparent_border_is_ignored_and_zero_alpha_additive_light_is_preserved(self):
        result = self.page.evaluate("""() => {
            const pixels = new Uint8Array(8 * 6 * 4);
            pixels[(2 * 8 + 3) * 4 + 3] = 255;
            const normal = EncyclopediaPortraitFraming.pixelBounds(pixels, 8, 6);
            pixels[(4 * 8 + 6) * 4] = 1;
            return {normal, additive: EncyclopediaPortraitFraming.pixelBounds(pixels, 8, 6),
                blank: EncyclopediaPortraitFraming.pixelBounds(new Uint8Array(16), 2, 2)};
        }""")
        self.assertEqual(result["normal"], {"left": 3, "bottom": 2, "right": 4, "top": 3})
        self.assertEqual(result["additive"], {"left": 3, "bottom": 2, "right": 7, "top": 5})
        self.assertIsNone(result["blank"])

    def test_key_switches_and_endpoints_are_sampled_beyond_regular_frame_grid(self):
        result = self.page.evaluate("""() => EncyclopediaPortraitFraming.animationSampleTimes({
            duration: 2, timelines: [{getFrameEntries: () => 2, frames: [0, 5, .123456, 8, 2, 9]}]
        })""")
        self.assertEqual(result[0], 0)
        self.assertEqual(result[-1], 2)
        self.assertIn(.123456, result)
        self.assertIn(.123455, result)
        self.assertGreaterEqual(len(result), 123)

    def test_source_hash_mismatch_is_rejected(self):
        result = self.page.evaluate("""async () => {
            const bytes = new TextEncoder().encode('source');
            const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
                .map(x => x.toString(16).padStart(2, '0')).join('');
            const profile = {sources: {'H001.skel': {bytes: bytes.length, sha256}}};
            await EncyclopediaPortraitFraming.verifySource(profile, 'H001.skel', bytes);
            bytes[0] = 1;
            try { await EncyclopediaPortraitFraming.verifySource(profile, 'H001.skel', bytes); return false; }
            catch { return true; }
        }""")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
