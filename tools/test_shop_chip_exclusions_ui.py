"""Real secret-shop checkbox logic with an offline, six-part catalog."""
import json
from pathlib import Path
import unittest
from playwright.sync_api import sync_playwright
from backend.tasks import BUY_TARGETS

ROOT=Path(__file__).resolve().parents[1]
SOURCE=(ROOT/'frontend/app.js').read_text()


def function(name):
    start=SOURCE.index(f'function {name}(')
    return SOURCE[start:SOURCE.index('\n}',start)+2]


class ChipExclusionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(channel='chrome',headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844})
        self.context.route('**/*',lambda route:route.abort())
        self.page=self.context.new_page();self.page.set_default_timeout(4000);self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.set_content('<html data-theme="dark"><body><main id="widgets" style="padding:12px;height:100vh;overflow:auto"></main></body></html>')
        self.page.add_style_tag(path=str(ROOT/'frontend/style.css'))
        script=SOURCE[SOURCE.index('const SHOP_LS ='):SOURCE.index('// 一轮刷新的产出统计')]
        self.page.add_script_tag(path=str(ROOT / 'frontend/action-wait.js'))
        self.page.add_script_tag(content='const BUY_TARGETS='+json.dumps(BUY_TARGETS,ensure_ascii=False)+';'+'''
          let activeAccount='alpha';const $=id=>document.getElementById(id),values={};
          const frontendStorageGet=key=>values[key], frontendStorageSet=(key,value)=>{values[key]=value;};
          const EQUIP_FILTER_SCHEMES_DATA=[],shopRunning={},shopStopRequested={};
          const taskIconHTML=()=>'',secretShopStateHTML=()=>'',activeAcc=()=>null;
          const checkboxUIHTML=()=>'<span class="check-box" aria-hidden="true">✓</span>';
          const starSourceSchemeOptions=()=>[],bindDropdown=()=>{};
          const clampCount=v=>Math.max(1,Math.min(999,Number(v)||1));
        '''+function('filterEscape')+function('dropdownHTML')+script+'''
          function mount(){
            $('widgets').innerHTML=renderShopWidget({id:'shop_refresh_diamond',name:'秘密商店刷新(钻石)',desc:'自动购买所选目标'});
            bindShopWidget('shop_refresh_diamond');document.querySelector('.shop-body').classList.remove('hidden');
          }mount();
        ''')

    def tearDown(self):
        self.assertEqual(self.errors,[]);self.context.close()

    def toggle(self,selector):
        self.page.locator(selector).locator('..').click()

    def test_six_parts_stay_in_one_row_and_default_purchase_all(self):
        boxes=self.page.locator('[data-chip-exclude]')
        self.assertEqual(boxes.count(),6)
        positions=[boxes.nth(i).locator('..').bounding_box() for i in range(6)]
        self.assertEqual(len({round(pos['y']) for pos in positions}),1)
        self.assertTrue(all(positions[i]['x']<positions[i+1]['x'] for i in range(5)))
        self.assertEqual(self.page.evaluate('shopWantedIds()'),['5','6','EC11','EC21','EC31','EC41','EC51','EC61'])
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
        folder=ROOT/'build/windows-feature-1027';folder.mkdir(parents=True,exist_ok=True)
        self.page.locator('.shop-widget').screenshot(path=str(folder/'chip-parts-mobile.png'))

    def test_multiple_exclusions_reach_purchase_list_and_survive_remount(self):
        self.toggle('[data-chip-exclude="EC11"]');self.toggle('[data-chip-exclude="EC51"]')
        self.assertEqual(self.page.evaluate('shopWantedIds()'),['5','6','EC21','EC31','EC41','EC61'])
        self.page.evaluate('mount()')
        self.assertTrue(self.page.locator('[data-chip-exclude="EC11"]').is_checked())
        self.assertTrue(self.page.locator('[data-chip-exclude="EC51"]').is_checked())

    def test_master_toggle_hides_parts_and_restores_choices(self):
        self.toggle('[data-chip-exclude="EC41"]');self.toggle('[data-wkey="chip"]')
        self.assertFalse(self.page.locator('.shop-chip-exclusions').is_visible())
        self.assertEqual(self.page.evaluate('shopWantedIds()'),['5','6'])
        self.toggle('[data-wkey="chip"]')
        self.assertTrue(self.page.locator('.shop-chip-exclusions').is_visible())
        self.assertTrue(self.page.locator('[data-chip-exclude="EC41"]').is_checked())
        self.assertNotIn('EC41',self.page.evaluate('shopWantedIds()'))

    def test_all_parts_can_be_excluded_and_accounts_stay_separate(self):
        for part in BUY_TARGETS[2]['parts']:self.toggle(f'[data-chip-exclude="{part["id"]}"]')
        self.assertEqual(self.page.evaluate('shopWantedIds()'),['5','6'])
        self.page.evaluate("activeAccount='beta';mount();")
        self.assertEqual(len(self.page.evaluate('shopWantedIds()')),8)
        self.page.evaluate("activeAccount='alpha';mount();")
        self.assertEqual(self.page.evaluate('shopWantedIds()'),['5','6'])

    def test_gvg_is_hidden_and_implementation_files_remain(self):
        self.page.set_content((ROOT/'frontend/index.html').read_text())
        self.page.add_style_tag(path=str(ROOT/'frontend/style.css'))
        self.assertFalse(self.page.locator('[data-section="gvg"]').is_visible())
        self.assertEqual(self.page.locator('#section-gvg').count(),1)
        self.assertEqual(self.page.locator('script[src*="gvg.js"]').count(),1)
        self.assertTrue((ROOT/'frontend/gvg.js').is_file())
        self.assertTrue((ROOT/'backend/gvg/api.py').is_file())


if __name__=='__main__':unittest.main()
