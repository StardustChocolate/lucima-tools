"""Offline publication contracts and end-to-end builds from an isolated source."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from build_cloud_encyclopedia import build as build_cloud
from build_encyclopedia import build_bundle
from build_encyclopedia_release import build_release
from encyclopedia_release import PLAN_NAME, collect_release, load_plan, read, save_plan
from stage_encyclopedia_cdn import stage_cdn
import test_encyclopedia_catalog as fixtures


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_cards(pool, version="cards1", heroes=("H001", "H002")):
    rows = []
    for hero in heroes:
        raw = (hero + "-finished-card").encode()
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"objects/{digest}.png"
        path = pool / "cards" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        rows.append({"id": hero, "image": {"path": relative, "size": len(raw), "sha256": digest}})
    kind = "lucima-encyclopedia-cards"
    relative = f"indexes/{version}.json"
    write_json(pool / "cards" / relative, {"schema": 1, "kind": kind, "version": version,
                                         "count": len(rows), "characters": rows})
    raw = (pool / "cards" / relative).read_bytes()
    write_json(pool / "cards/manifest.json", {"schema": 1, "kind": kind, "version": version,
               "baseUrl": "./", "count": len(rows), "index": {"path": relative,
               "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}})


class EncyclopediaReleaseTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.EncyclopediaCatalogTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.bundle = self.root / "bundle/encyclopedia"
        self.pool = self.root / "pool"
        self.avatars = self.root / "avatars"
        self.avatars.mkdir()
        (self.avatars / "H001.png").write_bytes(b"current-avatar")
        build_bundle(self.root, self.bundle)
        stage_cdn(self.root, self.pool, "media1")
        build_cloud(self.bundle, self.pool, self.avatars, "data1")
        self.enterContext(redirect_stdout(io.StringIO()))

    def test_plan_uses_generated_roster_and_only_selected_entries(self):
        make_cards(self.pool)
        release = collect_release(self.pool)
        self.assertEqual(release.counts["characters"], 2)
        self.assertNotIn("cards", release.versions)
        self.assertEqual([item.path for item in release.entries], ["manifest.json", "latest.json"])
        self.assertFalse(any(item.path.startswith("cards/") for item in release.files))
        release = collect_release(self.pool, cards=True)
        self.assertEqual(release.counts["cards"], 2)
        self.assertEqual(release.entries[-1].path, "latest.json")
        save_plan(release)
        self.assertEqual(load_plan(self.pool).document(), release.document())

    def test_corrupt_referenced_file_and_snapshot_are_rejected(self):
        release = collect_release(self.pool)
        save_plan(release)
        target = self.pool / release.files[0].path
        original = target.read_bytes()
        target.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_plan(self.pool)
        target.write_bytes(original)
        (self.pool / "versions/data1.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            load_plan(self.pool)

    def test_a_changed_dataset_invalidates_the_previous_publication_plan(self):
        save_plan(collect_release(self.pool))
        build_cloud(self.bundle, self.pool, self.avatars, "data2")
        with self.assertRaisesRegex(ValueError, "changed"):
            load_plan(self.pool)

    def test_traversal_is_rejected_before_reading_an_unrelated_file(self):
        data = read(self.pool / "latest.json")
        data["index"]["path"] = "../private.pem"
        write_json(self.pool / "latest.json", data)
        with self.assertRaisesRegex(ValueError, "Invalid object"):
            collect_release(self.pool)

    def test_incomplete_or_different_card_roster_cannot_be_published(self):
        make_cards(self.pool, heroes=("H001",))
        with self.assertRaisesRegex(ValueError, "collection"):
            collect_release(self.pool, cards=True)
        make_cards(self.pool, heroes=("H001", "H003"))
        with self.assertRaisesRegex(ValueError, "collection"):
            collect_release(self.pool, cards=True)

    def test_unified_build_uses_source_avatars_and_optional_card_renderer(self):
        for hero in ("H001", "H002"):
            directory = self.root / f"out/assets/团员/头像/{hero}_name"
            directory.mkdir(parents=True)
            (directory / f"Icon_Head_S_{hero}.png").write_bytes((hero + "-new-avatar").encode())
        args = SimpleNamespace(output=self.root / "unified", version="new1", cards=True,
                               from_existing=False, ark2_root=self.root, reuse_media=False,
                               work_dir=self.root / "work")
        def render(options):
            make_cards(options.resources, version=options.version)
        with patch("build_encyclopedia_cards.build", side_effect=render):
            release = build_release(args)
        self.assertEqual(release.versions, {"media": "new1", "data": "new1", "cards": "new1"})
        index = read(args.output / read(args.output / "latest.json")["index"]["path"])
        self.assertEqual((args.output / index["characters"][1]["avatar"]["path"]).read_bytes(), b"H002-new-avatar")
        self.assertEqual(load_plan(args.output).counts["cards"], 2)

    def test_existing_output_mode_does_not_rebuild_or_read_the_source(self):
        args = SimpleNamespace(output=self.pool, version=None, cards=False, from_existing=True)
        with patch("build_encyclopedia_release.build_bundle", side_effect=AssertionError("unexpected rebuild")):
            release = build_release(args)
        self.assertEqual(release.version, "data1")
        self.assertTrue((self.pool / PLAN_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
