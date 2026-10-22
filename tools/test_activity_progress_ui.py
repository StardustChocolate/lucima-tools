"""Activity tabs, live battle window and mobile layout with an offline API."""
from copy import deepcopy
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "frontend/app.js").read_text(encoding="utf-8")


def battle_snapshot():
    return {"sceneId": "BH610_1_13", "wave": 0,
            "allies": [{"id": f"1-0-{i}", "name": f"我方角色{i}", "avatar": f"/assets/avatars/H{i + 1:03}.png",
                        "hpPercent": 100 if i else 0, "dead": i == 0} for i in range(4)],
            "enemies": [{"id": f"2-0-{i}", "name": f"敌方角色{i}", "avatar": "/assets/avatars/H009.png",
                         "hpPercent": 52.5 if i == 0 else 100, "dead": False} for i in range(6)]}


def function_source(name):
    start = SOURCE.index(f"function {name}(")
    return SOURCE[start:SOURCE.index("\n}", start) + 2]


class ActivityProgressUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.context.route("**/*", lambda route: route.abort())
        self.context.route("**/assets/avatars/*", lambda route: route.fulfill(
            body=(ROOT / "assets/avatars" / Path(urlsplit(route.request.url).path).name).read_bytes(),
            content_type="image/png"))
        self.page = self.context.new_page()
        self.page.set_default_timeout(3000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.set_content('<base href="https://activity-progress.invalid"><main id="widgets" style="padding:16px"></main>')
        self.page.add_style_tag(path=str(ROOT / "frontend/style.css"))
        activity = SOURCE[SOURCE.index('const ACT_LS ='):SOURCE.index('// ---------- 商店购买自定义控件')]
        shared = "\n".join(function_source(name) for name in (
            "filterEscape", "dropdownHTML", "bindDropdown", "heroPortraitHTML", "abyssBattleHTML"))
        self.page.add_script_tag(content="""
          let activeAccount = 'alpha';
          const $ = id => document.getElementById(id);
          const values = {}, calls = [], logs = [];
          const frontendStorageGet = key => values[key];
          const frontendStorageSet = (key, value) => {values[key] = value;};
          const taskIconHTML = () => '';
          const checkboxUIHTML = () => '';
          const log = (...args) => logs.push(args);
          const logTo = (...args) => logs.push(args);
          const logResult = (...args) => logs.push(args);
          const applyStatus = () => {};
          const refreshAccountStatus = () => {};
          const clampCount = value => Math.min(999, Math.max(1, Number(value) || 1));
          const TASKS = [{id:'activity_scene', name:'活动关卡', desc:'选择当期活动、队伍与助战好友，按次数扫荡或自动推进已开放关卡'}];
          const DATA = {scenes:[{id:'BH610_1_12', name:'困难'}],
            teams:[{id:'T', name:'我的队伍'}], supports:[], favorites:[],
            activities:[{id:'BH610',name:'活动测试',completed:2,total:13}]};
          let finishRun, finishOptions, holdOptions = false, failOptions = false;
          const api = async (path, body) => {
            calls.push({path, body, account: activeAccount});
            if (path === '/api/activity/options') {
              if (failOptions) throw new Error('offline');
              if (holdOptions) return new Promise(resolve => {finishOptions = resolve;});
              return DATA;
            }
            if (path === '/api/activity/progress/run') return new Promise(resolve => {finishRun = resolve;});
            return {ok:true};
          };
        """ + shared + activity + """
          function mount() {
            $('widgets').innerHTML = renderActivityWidget(TASKS[0]);
            bindActivityWidget('activity_scene');
            $('act-activity_scene').querySelector('.shop-body').classList.remove('hidden');
          }
          mount();
          loadActivityOptions('activity_scene');
        """)

    def tearDown(self):
        self.context.close()

    def select_progress(self):
        self.page.click('[data-act-mode="progress"]')

    def publish_battle(self, battle=None, *, running=True):
        self.page.evaluate("snapshot => applyActivityProgress('alpha', snapshot)", {
            "running": running, "progress": {"completed": 12, "total": 13,
                "stageName": "精英关卡", "detail": "挑战中" if running else "挑战完成", "battle": battle}})

    def test_mode_switch_uses_common_styles_and_hides_sweep_only_controls(self):
        self.assertEqual(self.page.locator('.act-start').inner_text(), '▶ 开始扫荡')
        self.select_progress()
        self.assertTrue(self.page.locator('.act-family-slot').is_visible())
        self.assertFalse(self.page.locator('.act-scene-slot').is_visible())
        self.assertFalse(self.page.locator('.act-count').is_visible())
        self.assertFalse(self.page.locator('.act-quick-row').is_visible())
        self.assertFalse(self.page.locator('.act-urgent-control').is_visible())
        self.assertEqual(self.page.locator('.act-start').inner_text(), '▶ 开始推图')
        self.page.click('[data-act-mode="sweep"]')
        self.assertTrue(self.page.locator('.act-count').is_visible())
        self.assertTrue(self.page.locator('.act-scene-slot').is_visible())
        self.assertEqual(self.errors, [])

    def test_run_cancel_progress_and_completion_stay_scoped_to_the_originating_account(self):
        self.select_progress()
        self.page.click('.act-start')
        call = self.page.evaluate('calls.at(-1)')
        self.assertEqual(call['path'], '/api/activity/progress/run')
        self.assertEqual(call['body'], {'account': 'alpha', 'family': 'BH610', 'teamId': 'T', 'supportCuid': None})
        self.page.evaluate("applyActivityProgress('alpha', {running:true,progress:{completed:3,total:13,stageName:'关卡4',detail:'挑战中',battle:{wave:2}}})")
        self.assertIn('第 3 波', self.page.locator('.act-progress-status').inner_text())
        self.assertTrue(self.page.locator('.act-battle-progress').is_visible())
        self.page.evaluate("activeAccount = 'beta'; mount();")
        self.assertEqual(self.page.locator('.act-start').inner_text(), '▶ 开始扫荡')
        self.page.evaluate("activeAccount = 'alpha'; mount();")
        self.assertEqual(self.page.locator('.act-start').inner_text(), '停止推图')
        self.page.click('.act-start')
        self.assertEqual(self.page.evaluate('calls.at(-1).body.account'), 'alpha')
        self.assertEqual(self.page.evaluate('calls.at(-1).path'), '/api/activity/progress/cancel')
        self.assertEqual(self.page.locator('.act-start').inner_text(), '停止中…')
        self.page.evaluate("finishRun({ok:false,detail:'自动推图已停止',activityProgress:{running:false,progress:{completed:3,total:13,detail:'自动推图已停止'}}})")
        self.page.wait_for_function("!actProgressRunning.alpha")
        self.assertIn('已停止', self.page.locator('.act-progress-status').inner_text())
        self.assertEqual(self.page.locator('.act-start').inner_text(), '▶ 开始推图')
        self.assertFalse(self.page.locator('.act-battle-progress').is_visible())
        self.assertEqual(self.errors, [])

    def test_battle_window_updates_health_and_waves_and_restores_only_its_account(self):
        self.select_progress()
        self.publish_battle()
        panel = self.page.locator('.act-battle-progress')
        self.assertFalse(panel.is_visible())
        battle = battle_snapshot()
        self.publish_battle(battle)
        self.assertTrue(panel.is_visible())
        self.assertEqual(panel.locator('.abyss-battle-scene').inner_text(), '精英关卡')
        self.assertEqual(panel.locator('.abyss-battle-wave').inner_text(), '第 1 波')
        self.assertEqual(panel.locator('.enemies .abyss-battle-member').count(), 6)
        self.assertEqual(panel.locator('.allies .abyss-battle-member').count(), 4)
        self.assertEqual(panel.locator('.enemies [role=progressbar]').first.get_attribute('aria-valuenow'), '52.5')
        self.assertIn('dead', panel.locator('.allies .abyss-battle-member').first.get_attribute('class'))
        self.page.evaluate("window.firstEnemy = document.querySelector('.act-battle-progress .enemies .abyss-battle-member')")
        self.publish_battle(deepcopy(battle))
        self.assertTrue(self.page.evaluate("firstEnemy === document.querySelector('.act-battle-progress .enemies .abyss-battle-member')"))
        battle.update(wave=2, enemies=[{**battle['enemies'][0], 'id': '2-2-0', 'hpPercent': 20}])
        self.publish_battle(battle)
        self.assertEqual(panel.locator('.abyss-battle-wave').inner_text(), '第 3 波')
        self.assertEqual(panel.locator('.enemies .abyss-hp-percent').inner_text(), '20%')
        self.page.evaluate("activeAccount = 'beta'; mount()")
        self.assertFalse(panel.is_visible())
        self.publish_battle(battle)
        self.assertFalse(panel.is_visible())
        self.page.evaluate("activeAccount = 'alpha'; mount()")
        self.assertTrue(panel.is_visible())
        self.assertEqual(panel.locator('.abyss-battle-wave').inner_text(), '第 3 波')
        self.publish_battle()
        self.assertFalse(panel.is_visible(), 'Returning to a non-WebSocket stage clears the battle window')
        self.assertEqual(panel.locator('.abyss-battle-member').count(), 0)
        self.publish_battle(battle)
        self.publish_battle(battle, running=False)
        self.assertFalse(panel.is_visible(), 'Ending the run hides even a stale final battle snapshot')
        self.assertEqual(self.errors, [])

    def test_live_battle_window_fits_mobile_and_desktop_in_both_themes(self):
        self.select_progress()
        self.publish_battle(battle_snapshot())
        panel = self.page.locator('.act-battle-progress')
        for theme in ('dark', 'light'):
            for width in (360, 390, 1280):
                with self.subTest(theme=theme, width=width):
                    self.page.set_viewport_size({'width': width, 'height': 950})
                    self.page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                    self.assertTrue(panel.is_visible())
                    self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    self.assertTrue(panel.evaluate("e => Array.from(e.querySelectorAll('.abyss-battle-member')).every(card => { const r=card.getBoundingClientRect(), p=e.getBoundingClientRect(); return r.left >= p.left && r.right <= p.right; })"))
        self.page.set_viewport_size({'width': 390, 'height': 950})
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        panel.screenshot(path=str(ROOT / 'previews/activity-battle-mobile.png'))
        self.assertEqual(self.errors, [])

    def test_mobile_light_dark_layout_and_persisted_mode(self):
        self.select_progress()
        for theme in ('dark', 'light'):
            for width in (360, 390, 1280):
                self.page.set_viewport_size({'width': width, 'height': 950})
                self.page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                self.page.evaluate('mount()')
                self.assertTrue(self.page.locator('.act-family-slot').is_visible())
                self.assertFalse(self.page.locator('.act-count').is_visible())
                self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.page.set_viewport_size({'width': 390, 'height': 950})
        self.page.evaluate("() => { mount(); return loadActivityOptions('activity_scene'); }")
        self.page.screenshot(path=str(ROOT / 'previews/activity-progress-mobile.png'), full_page=True)
        self.assertEqual(self.errors, [])

    def test_options_from_previous_account_cannot_overwrite_current_config(self):
        self.page.evaluate("() => { holdOptions=true; void loadActivityOptions('activity_scene',true); }")
        self.page.evaluate("activeAccount='beta'; mount(); finishOptions(DATA)")
        self.page.wait_for_timeout(30)
        self.assertEqual(self.page.evaluate('getActCfg().family'), '')
        self.assertEqual(self.page.evaluate('getActCfg().teamId'), '')
        self.assertEqual(self.errors, [])

    def test_progress_tab_has_its_own_retry_when_options_fail(self):
        self.select_progress()
        self.page.evaluate("failOptions = true")
        self.page.evaluate("() => loadActivityOptions('activity_scene', true)")
        self.assertTrue(self.page.locator('.act-family-slot .act-options-retry').is_visible())
        self.page.evaluate("failOptions = false")
        self.page.click('.act-family-slot .act-options-retry')
        self.assertTrue(self.page.locator('.act-family .dd-cur').is_visible())
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
