"""Official art, legacy catalog upgrades and shared-currency labels."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend import appcore, recruitment
from backend.inventory_metadata import labels,build

ROOT=Path(__file__).resolve().parents[1]


class InventoryImages(unittest.TestCase):
    def test_screenshot_artifacts_and_material_roles_have_native_pngs(self):
        for sid in ('A0007','A0131'):
            self.assertEqual(recruitment.catalog()['results'][sid]['icon'],f'/assets/items/{sid}.png')
            self.assertTrue((ROOT/f'assets/items/{sid}.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        for sid in ('M0023','M0191','M0192','M0193'):
            url=appcore.avatar_url(sid)
            self.assertEqual(url,f'/assets/avatars/{sid}.png')
            self.assertTrue((ROOT/url.lstrip('/')).is_file())

    def test_all_material_role_items_have_bundled_portraits(self):
        raw=ROOT/'build/season-refresh-1028/source/文本/数据表/sd_Item.txt'
        if not raw.exists():self.skipTest('Official source table is only available in the build workspace')
        lines=raw.read_text().splitlines();headers=lines[0].split('@')
        ids=[row['ID'] for line in lines[1:] if (row:=dict(zip(headers,line.split('@')))).get('ItemType')=='Role' and row['ID'].startswith('M')]
        self.assertEqual(len(ids),58)
        self.assertTrue(all(appcore.avatar_url(sid) for sid in ids))

    def test_old_active_resource_catalog_can_use_new_bundled_art(self):
        data=deepcopy(recruitment.catalog());data['results']['A0007']['icon']=None
        with patch.object(recruitment.json,'loads',return_value=data):
            self.assertEqual(recruitment.catalog()['results']['A0007']['icon'],'/assets/items/A0007.png')

    def test_shared_ap_icons_keep_actual_currency_identity(self):
        raw=[{'StaticID':sid,'Count':i+5} for i,sid in enumerate(('40','41','54','69'))]
        before=deepcopy(raw);items={r['id']:r for r in appcore._roster_items(raw)}
        self.assertEqual(items['40']['name'],'第1章贡献度')
        self.assertEqual(items['41']['label'],'第2章')
        self.assertEqual(items['54']['label'],'贡献·54')
        self.assertEqual(items['69']['name'],'召唤里程积分')
        self.assertEqual(items['54']['count'],7)
        self.assertTrue(all(r['category']=='other' for r in items.values()))
        self.assertEqual(raw,before)

    def test_game_activity_currencies_are_wealth_with_individual_images(self):
        ids=('AH002','AH175','AH188','AH120','AH806')
        items=appcore._roster_items([{'StaticID':sid,'Count':1} for sid in ids])
        self.assertTrue(all(r['category']=='wealth' for r in items))
        self.assertEqual(len({(ROOT/f'assets/items/{sid}.png').read_bytes() for sid in ids}),len(ids))

    def test_label_builder_uses_official_description_and_does_not_guess_missing_chapters(self):
        data=build([{'ID':'x','Name':'n','Description':'d','Icon':'Icon/Item[AP]'},
                    {'ID':'y','Name':'n','Icon':'Icon/Item[AP]'},
                    {'ID':'z','Name':'n','Icon':'Icon/Item[Coin]'}],{'n':'贡献度','d':'第8章贡献度'})
        self.assertEqual(data['items'],{'x':{'name':'第8章贡献度','label':'第8章'},'y':{'name':'贡献度','label':'贡献·y'}})


if __name__=='__main__':unittest.main()
