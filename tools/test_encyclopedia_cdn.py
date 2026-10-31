"""Content pool reuse, immutable snapshots and publication ordering."""
from __future__ import annotations

import hashlib
import json
import unittest

from stage_encyclopedia_cdn import stage_cdn
import test_encyclopedia_catalog as catalog_tests


class EncyclopediaCDNTests(unittest.TestCase):
    setUp = catalog_tests.EncyclopediaCatalogTests.setUp
    write_index = catalog_tests.EncyclopediaCatalogTests.write_index

    def manifest(self):
        return json.loads((self.root / "cdn" / "manifest.json").read_text(encoding="utf-8"))

    def test_pool_deduplicates_identical_images_and_preserves_atlas_names(self):
        portrait = self.source / "立绘" / "H001.png"
        icon = self.source / "技能图标" / "Icon_Skill_Test.png"
        icon.write_bytes(portrait.read_bytes())
        report = stage_cdn(self.root, self.root / "cdn", "v1")
        manifest = self.manifest()
        self.assertEqual((report["logicalFiles"], report["objects"]), (6, 5))
        self.assertEqual(manifest["files"]["portraits/static/H001.png"],
                         manifest["files"]["skills/Icon_Skill_Test.png"])
        texture = manifest["files"]["portraits/spine/H001/CG_H001_a.png"]
        self.assertEqual(texture["sha256"], hashlib.sha256(b"texture").hexdigest())
        self.assertEqual((self.root / "cdn/manifest.json").read_bytes(),
                         (self.root / "cdn/manifests/v1.json").read_bytes())
        self.assertNotIn("portraits/static/H002.png", manifest["files"])

    def test_next_version_reuses_all_unchanged_files_and_only_adds_changed_icon(self):
        first = stage_cdn(self.root, self.root / "cdn", "v1")
        previous = self.manifest()
        second = stage_cdn(self.root, self.root / "cdn", "v2")
        self.assertEqual(second["newObjects"], [])
        self.assertEqual(second["changedFiles"], [])
        self.assertEqual(previous["files"], self.manifest()["files"])
        (self.source / "技能图标/Icon_Skill_Test.png").write_bytes(b"new icon")
        third = stage_cdn(self.root, self.root / "cdn", "v3")
        self.assertEqual(len(third["newObjects"]), 1)
        self.assertEqual(third["newObjectBytes"], len(b"new icon"))
        self.assertEqual(third["changedFiles"], ["skills/Icon_Skill_Test.png"])
        self.assertEqual(len(list((self.root / "cdn/objects").iterdir())), first["objects"] + 1)
        self.assertEqual(previous, json.loads((self.root / "cdn/manifests/v1.json").read_text()))

    def test_same_version_cannot_replace_different_content(self):
        stage_cdn(self.root, self.root / "cdn", "v1")
        original = (self.root / "cdn/manifest.json").read_bytes()
        (self.source / "技能图标/Icon_Skill_Test.png").write_bytes(b"new icon")
        with self.assertRaises(FileExistsError):
            stage_cdn(self.root, self.root / "cdn", "v1")
        self.assertEqual((self.root / "cdn/manifest.json").read_bytes(), original)

    def test_corrupt_existing_object_does_not_publish_new_manifest(self):
        stage_cdn(self.root, self.root / "cdn", "v1")
        original = (self.root / "cdn/manifest.json").read_bytes()
        file = self.manifest()["files"]["portraits/static/H001.png"]
        (self.root / f"cdn/objects/{file['sha256']}.png").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            stage_cdn(self.root, self.root / "cdn", "v2")
        self.assertEqual((self.root / "cdn/manifest.json").read_bytes(), original)
        self.assertFalse((self.root / "cdn/manifests/v2.json").exists())


if __name__ == "__main__":
    unittest.main()
