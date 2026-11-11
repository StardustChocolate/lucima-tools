"""Official summon thumbnails refresh stale assets without altering the artwork."""
import csv
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from tools import sync_assets


class SummonAssetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source, self.target = self.root / 'source', self.root / 'assets'
        self.icon = self.source / '羁绊/图标/official_small_sprite.png'
        self.icon.parent.mkdir(parents=True)
        with (self.source / '羁绊/列表.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['ID', '有图', '图标文件'])
            writer.writeheader()
            writer.writerows([
                {'ID': 'A0443', '有图': 'True', '图标文件': 'assets/羁绊/图标/official_small_sprite.png'},
                {'ID': 'A9998', '有图': 'False', '图标文件': ''},
            ])

    def test_official_sprite_refreshes_placeholder_and_repeated_sync_is_unchanged(self):
        image = Image.new('RGBA', (128, 128), (10, 200, 30, 255))
        image.putpixel((0, 0), (0, 0, 0, 0))
        image.save(self.icon)
        Image.new('RGB', (128, 128), 'red').save(self.icon.with_name('A0443_decoy.png'))
        target = self.target / 'gacha/artifacts'
        target.mkdir(parents=True)
        output, legacy = target / 'A0443.webp', target / 'legacy.webp'
        output.write_bytes(b'old placeholder')
        legacy.write_bytes(b'historical image')

        files = sync_assets._artifact_files(self.source)
        self.assertEqual({'A0443.webp'}, set(files))
        sync_assets._sync_category(self.target, 'gacha/artifacts', files, preserve_existing=True)
        with Image.open(BytesIO(output.read_bytes())) as thumbnail:
            self.assertEqual('WEBP', thumbnail.format)
            self.assertEqual((112, 112), thumbnail.size)
            self.assertGreater(thumbnail.getpixel((56, 56))[1], 190)
            self.assertLess(thumbnail.getpixel((0, 0))[3], 255)
        self.assertEqual(b'historical image', legacy.read_bytes())
        timestamp = output.stat().st_mtime_ns
        sync_assets._sync_category(self.target, 'gacha/artifacts', sync_assets._artifact_files(self.source), preserve_existing=True)
        self.assertEqual(timestamp, output.stat().st_mtime_ns)

    def test_declared_missing_sprite_fails_before_any_runtime_asset_changes(self):
        self.target.mkdir()
        old = self.target / 'keep.png'
        old.write_bytes(b'existing asset')
        with patch.object(sync_assets, '_localization_files', return_value={}), \
                patch.object(sync_assets, '_avatar_files', return_value=({}, [])):
            with self.assertRaisesRegex(RuntimeError, 'missing or invalid artifact icon'):
                sync_assets.sync(self.source, self.target, preserve_existing_images=True)
        self.assertEqual([old], list(self.target.iterdir()))
        self.assertEqual(b'existing asset', old.read_bytes())


if __name__ == '__main__':
    unittest.main()
