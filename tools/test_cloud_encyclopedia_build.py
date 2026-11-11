"""The cloud publisher preserves renderer data and reuses immutable objects."""
import json
import tempfile
import unittest
from pathlib import Path

import test_encyclopedia_catalog as fixtures
from build_cloud_encyclopedia import build
from build_encyclopedia import build_bundle
from stage_encyclopedia_cdn import stage_cdn


class CloudBuildTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EncyclopediaCatalogTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.bundle = self.root / 'bundle/encyclopedia'
        self.pool = self.root / 'pool'
        self.avatars = self.root / 'avatars'
        self.avatars.mkdir()
        (self.avatars / 'H001.png').write_bytes(b'avatar')
        build_bundle(self.fixture.root, self.bundle)
        stage_cdn(self.fixture.root, self.pool, 'media1')

    def read(self, ref):
        return json.loads((self.pool / ref['path']).read_bytes())

    def test_cloud_data_keeps_character_fields_and_download_references_separate(self):
        build(self.bundle, self.pool, self.avatars, 'data1')
        entry = json.loads((self.pool / 'latest.json').read_bytes())
        index = self.read(entry['index'])
        self.assertEqual(len(index['characters']), 2)
        self.assertEqual(index['search']['ids']['h001'], ['H001'])
        self.assertEqual(index['search']['aliases']['甲别称'], ['H001'])
        self.assertEqual(index['search']['aliases']['测试外号'], ['H001'])
        self.assertEqual(index['search']['pinyin']['jiabiecheng'], ['H001'])
        hero = index['characters'][0]
        detail = self.read(hero['detail'])
        original = json.loads((self.bundle / 'characters/H001.json').read_bytes())
        self.assertEqual(detail['character'], original)
        self.assertEqual(set(detail['media']), {
            'skills/Icon_Skill_Test.png', 'portraits/static/H001.png',
            'portraits/spine/H001/H001.atlas', 'portraits/spine/H001/H001.skel',
            'portraits/spine/H001/CG_H001_a.png'})
        self.assertFalse(any(path.endswith('.js') for path in detail['media']))
        self.assertLess((self.pool / 'latest.json').stat().st_size, 2048)

    def test_new_dataset_reuses_unchanged_characters_and_never_rewrites_a_version(self):
        build(self.bundle, self.pool, self.avatars, 'data1')
        first = (self.pool / 'latest.json').read_bytes()
        old = self.read(json.loads(first)['index'])
        target = self.bundle / 'characters/H001.json'
        data = json.loads(target.read_bytes())
        data['Description'] = 'official source update fixture'
        target.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            build(self.bundle, self.pool, self.avatars, 'data1')
        self.assertEqual((self.pool / 'latest.json').read_bytes(), first)
        build(self.bundle, self.pool, self.avatars, 'data2')
        new = self.read(json.loads((self.pool / 'latest.json').read_bytes())['index'])
        self.assertNotEqual(old['characters'][0]['detail'], new['characters'][0]['detail'])
        self.assertEqual(old['characters'][1]['detail'], new['characters'][1]['detail'])
        self.assertEqual((self.pool / 'versions/data1.json').read_bytes(), first)

    def test_documented_gap_preserves_source_skill_reference_but_unlisted_gap_fails(self):
        path = 'skills/Icon_Skill_Test.png'
        manifest = json.loads((self.pool / 'manifest.json').read_bytes())
        del manifest['files'][path]
        (self.pool / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(KeyError):
            build(self.bundle, self.pool, self.avatars, 'gap1')
        manifest['missingFiles'] = {path: 'Official source reference has no image'}
        (self.pool / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        build(self.bundle, self.pool, self.avatars, 'gap1')
        index = self.read(json.loads((self.pool / 'latest.json').read_bytes())['index'])
        detail = self.read(index['characters'][0]['detail'])
        self.assertEqual(detail['character']['Skill'][0]['IconSprite'], 'Icon_Skill_Test')
        self.assertNotIn(path, detail['media'])
        self.assertEqual(detail['missingMedia'], manifest['missingFiles'])


if __name__ == '__main__':
    unittest.main()
