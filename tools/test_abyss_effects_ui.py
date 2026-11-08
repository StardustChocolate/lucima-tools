"""Abyss effect presentation with production frontend and offline game data."""
import json
import mimetypes
import os
import io
from zipfile import ZipFile, BadZipFile
from copy import deepcopy
from pathlib import Path
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.sync_api import sync_playwright

from backend.tasks import abyss_scenes, abyss_scene_effects, abyss_scene_enemies
from backend.appcore import _abyss_enemy_brief

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / os.environ.get('ABYSS_UI_REVIEW_DIR', 'build/abyss-intel-review')
ORIGIN = "https://abyss-effects.invalid"
TASK = {"id": "abyss_auto", "name": "虚构深渊", "desc": "配置队伍，自动进行战斗", "ui": "abyss"}


def options(activity):
    state = {"SceneDataContainer": {"Scenes": [
        {"StaticID": f"MysteriousRealm_{index}", "Stars": [1, 1, 0, 0]} for index in range(1, 11)]},
        "ActivityDBSettingContainer": {"SettingList": [
            {"ActivityID": sid, "IsIgnoreTime": True}
            for sid in ("Version_Fantasy", "Version_Fantasy01_1-6", activity)]}}
    return {"scenes": [{**scene, "effects": abyss_scene_effects(state, scene["id"]),
                        "enemies": {wave: [_abyss_enemy_brief(enemy) for enemy in enemies]
                                    for wave, enemies in abyss_scene_enemies(state, scene["id"]).items()}}
                       for scene in abyss_scenes(state)], "roles": []}


class AbyssEffectsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        chrome = Path(os.environ.get("CHROME_EXECUTABLE", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        cls.browser = cls.playwright.chromium.launch(headless=True, **({"executable_path": str(chrome)} if chrome.is_file() else {}))
        cls.payloads = {"first": options("Fantasy_H189"), "second": options("Fantasy_H610")}
        cls.apk = ZipFile(os.environ['ABYSS_UI_APK']) if os.environ.get('ABYSS_UI_APK') else None
        cls.apk_equipment = {}
        if cls.apk:
            for name in cls.apk.namelist():
                if name.startswith('assets/chaquopy/') and name.endswith('.imy'):
                    try:
                        source = ZipFile(io.BytesIO(cls.apk.read(name)))
                    except BadZipFile:
                        continue
                    with source:
                        for entry in source.namelist():
                            if entry.startswith('equipment/web/'):
                                cls.apk_equipment[entry] = source.read(entry)
        REVIEW.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        if cls.apk:
            cls.apk.close()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors, self.unexpected, self.requests = [], [], []
        self.responses = deepcopy(self.payloads)
        self.hold_account, self.pending = None, None
        self.fail_options = False
        self.runs=[];self.run_pending=None;self.hold_run=False;self.restore_calls=0
        self.run_response={'ok':True,'detail':'离线挑战完成','result':{'recoveryRequired':False}}
        self.run_status=200
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.serve)
        self.page.goto(ORIGIN)
        self.page.wait_for_function("typeof enterApp === 'function' && equipmentNotificationReady")
        self.page.evaluate("""task => {
            for (const account of ['first', 'second']) rememberAccount({account, status:{name:account}});
            activeAccount='first'; enterApp(); showSection('manual');
            renderManualPanel([task]); window.effectTask=task;
        }""", TASK)

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])

    def serve(self, route):
        url = urlsplit(route.request.url)
        path = unquote(url.path)
        self.requests.append(path)
        if url.netloc != urlsplit(ORIGIN).netloc:
            self.unexpected.append(route.request.url)
            route.abort()
            return
        if path == "/api/abyss/options":
            account = parse_qs(url.query)["account"][0]
            if account == self.hold_account:
                self.pending = route
                return
            route.fulfill(status=500 if self.fail_options else 200, content_type="application/json",
                          body=json.dumps({"detail": "读取失败"} if self.fail_options else self.responses[account]))
            return
        if path=='/api/abyss/run':
            self.runs.append(route.request.post_data_json)
            if self.hold_run:self.run_pending=route;return
            route.fulfill(status=self.run_status,content_type='application/json',body=json.dumps(self.run_response));return
        if path=='/api/abyss/restore':
            self.restore_calls+=1
            self.responses['first']['recoveryPending']=False
            route.fulfill(content_type='application/json',body=json.dumps({'ok':True,'detail':'原装备与羁绊已归还','recoveryPending':False}));return
        if path.startswith("/api/"):
            responses = {
                "/api/game-data/status": {"phase":"ready","version":"catalog_115942","revision":0},
                "/api/accounts": {"accounts": []}, "/api/preferences": {"settings": {}},
                "/api/config": {"platform": "windows", "version": "test"},
                "/api/tasks": {"toggle": [], "manual": [TASK], "buyTargets": {}},
                "/api/update/status": {"testServer": True, "available": False},
                "/api/update/check": {"testServer": True, "available": False},
                "/api/logs/auto": {"entries": [], "logSeq": 0},
            }
            if path not in responses:
                self.unexpected.append(path)
            route.fulfill(content_type="application/json", body=json.dumps(responses.get(path, {})))
            return
        file = ROOT / "frontend/index.html" if path == "/" else (
            ROOT / "frontend" / path.removeprefix("/static/") if path.startswith("/static/")
            else ROOT / "equipment/web" / path.removeprefix("/equipment/") if path.startswith("/equipment/")
            else ROOT / path.lstrip("/"))
        if self.apk:
            relative = file.relative_to(ROOT).as_posix()
            try:
                body = self.apk_equipment[relative] if relative.startswith('equipment/web/') else self.apk.read('assets/web/' + relative)
            except KeyError:
                self.unexpected.append('missing APK resource: ' + relative)
                route.abort()
                return
            route.fulfill(body=body, content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
            return
        if not file.resolve().is_relative_to(ROOT) or not file.is_file():
            self.unexpected.append(path)
            route.abort()
            return
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream")

    def open_options(self):
        self.page.locator(".abyss-toggle").click()
        self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loaded === '1'")

    def select_scene(self, floor):
        self.page.locator(".abyss-scene .dd-btn").click()
        self.page.locator(f'.abyss-scene .dd-opt[data-val="MysteriousRealm_{floor}"]').click()

    def test_collapsed_intel_has_one_visible_entry(self):
        self.page.set_viewport_size({'width':390,'height':900})
        self.page.evaluate('setDrawer(false)')
        self.open_options()
        panel = self.page.locator('.abyss-intel')
        panel.locator(':scope > summary').click()
        self.assertEqual(self.page.locator('.abyss-widget .shop-body > details').count(), 1)
        self.assertEqual(panel.locator(':scope > summary > span').first.inner_text(), '情报')
        self.assertEqual(self.page.locator('.abyss-effects,.abyss-enemies').count(), 0)
        panel.evaluate("e=>{const c=e.closest('.content');c.scrollTop+=e.getBoundingClientRect().top-c.getBoundingClientRect().top-10;}")
        self.page.screenshot(path=str(REVIEW / 'apk-intel-collapsed-390.png'), animations='disabled')

    def test_single_intel_groups_effects_and_enemies_per_wave_and_toggles_them_together(self):
        self.open_options()
        panel = self.page.locator('.abyss-intel')
        self.assertEqual(panel.count(), 1)
        self.assertEqual(self.page.locator('.abyss-effects,.abyss-enemies').count(), 0)
        self.assertEqual(panel.locator(':scope > summary > span').first.inner_text(), '情报')
        self.assertEqual(panel.locator('.abyss-intel-wave > h4').all_inner_texts(), ['上半', '下半'])
        for floor in (1, 7):
            if floor != 1:
                self.select_scene(floor)
            source = next(scene for scene in self.responses['first']['scenes'] if scene['id'] == f'MysteriousRealm_{floor}')
            for wave in ('upper', 'lower'):
                section = panel.locator(f'.abyss-intel-wave[data-wave={wave}]')
                self.assertEqual(section.locator('.abyss-intel-effects').count(), 1)
                self.assertEqual(section.locator('.abyss-intel-enemies').count(), 1)
                self.assertEqual(section.locator('.abyss-enemy').count(), len(source['enemies'][wave]))
                for group in ('bonuses', 'restrictions'):
                    self.assertEqual(section.locator(f'.{group} li').all_inner_texts(), [effect['description'] for effect in source['effects'][wave][group]])
                self.assertEqual(section.locator('.abyss-enemy').evaluate_all('nodes=>nodes.map(node=>node.dataset.enemyId)'), [enemy['staticId'] for enemy in source['enemies'][wave]])
                effects = section.locator('.abyss-intel-effects').bounding_box()
                enemies = section.locator('.abyss-intel-enemies').bounding_box()
                self.assertLessEqual(effects['y'] + effects['height'], enemies['y'])
        panel.locator(':scope > summary').click()
        self.assertFalse(panel.evaluate('node=>node.open'))
        for node in panel.locator('.abyss-intel-effects,.abyss-intel-enemies').all():
            self.assertTrue(node.is_hidden())
        self.page.locator('.abyss-options-refresh').click()
        self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loading === '0'")
        self.assertFalse(panel.evaluate('node=>node.open'), '刷新保留整个情报的收起状态')
        panel.locator(':scope > summary').click()
        for node in panel.locator('.abyss-intel-effects,.abyss-intel-enemies').all():
            self.assertTrue(node.is_visible())

    def test_selection_refresh_and_account_isolation(self):
        self.open_options()
        panel = self.page.locator(".abyss-intel")
        self.assertTrue(panel.evaluate("e => e.open"))
        self.assertIn("狙击", panel.locator(".bonuses").first.inner_text())
        self.assertEqual(panel.locator(".abyss-intel-wave > h4").all_inner_texts(), ["上半", "下半"])
        count = self.requests.count("/api/abyss/options")
        self.select_scene(7)
        self.assertEqual(panel.locator(".abyss-intel-scene").inner_text(), "第 7 关")
        self.assertIn("不屈", panel.locator(".bonuses").first.inner_text())
        self.assertIn("术师", panel.locator(".restrictions").first.inner_text())
        self.assertEqual(self.requests.count("/api/abyss/options"), count)
        panel.locator(":scope > summary").click()
        self.page.locator(".abyss-options-refresh").click()
        self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loading === '0'")
        self.assertFalse(panel.evaluate("e => e.open"), "Refreshing must preserve the user's collapse choice")
        self.hold_account = "first"
        self.page.locator(".abyss-options-refresh").click()
        self.page.wait_for_timeout(40)
        self.assertIsNotNone(self.pending)
        self.page.evaluate("activeAccount='second'; renderManualPanel([effectTask]); setAbyssCfg({sceneId:'MysteriousRealm_7'})")
        self.open_options()
        self.assertIn("护盾", self.page.locator(".abyss-intel .bonuses").first.inner_text())
        self.pending.fulfill(content_type="application/json", body=json.dumps(self.responses["first"]))
        self.assertIn("护盾", self.page.locator(".abyss-intel .bonuses").first.inner_text())
        self.assertNotIn("不屈", self.page.locator(".abyss-intel").inner_text())

    def test_missing_empty_and_failed_effects_never_leave_stale_text(self):
        first = self.responses["first"]["scenes"][0]
        first["effects"]["upper"]["bonuses"] = [
            {"id": "FG_Special_021", "description": "效果说明暂缺", "descriptionMissing": True},
            {"id": "literal", "description": "<img src=x onerror=alert(1)>", "descriptionMissing": False}]
        first["effects"]["lower"] = {"bonuses": [], "restrictions": []}
        self.open_options()
        panel = self.page.locator(".abyss-intel")
        self.assertEqual(panel.locator(".abyss-effect-missing").inner_text(), "效果说明暂缺")
        self.assertIn("FG_Special_021", panel.locator(".abyss-effect-missing").get_attribute("title"))
        self.assertIn("<img src=x onerror=alert(1)>", panel.inner_text())
        self.assertEqual(panel.locator(".abyss-intel-effects img").count(), 0)
        self.assertEqual(panel.locator(".abyss-effect-empty").all_inner_texts(), ["无", "无"])
        self.fail_options = True
        self.page.locator(".abyss-options-refresh").click()
        self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loading === '0'")
        self.assertTrue(panel.is_hidden())
        self.assertEqual(panel.locator(".abyss-intel-content").inner_html(), "")
        self.fail_options = False
        self.responses["first"]["scenes"] = []
        self.page.locator(".abyss-options-refresh").click()
        self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loading === '0'")
        self.assertTrue(panel.is_hidden())
        self.assertTrue(self.page.locator(".abyss-scene .dd-btn").is_disabled())

    def test_mobile_desktop_and_themes(self):
        for width in (320, 390, 1280):
            self.page.set_viewport_size({"width": width, "height": 900})
            self.page.evaluate("setDrawer(false); renderManualPanel([effectTask]); setAbyssCfg({sceneId:'MysteriousRealm_10'})")
            self.open_options()
            panel = self.page.locator(".abyss-intel")
            self.assertTrue(panel.evaluate("e => e.open"))
            for theme in ("dark", "light"):
                self.page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                metrics = panel.evaluate("""e => ({
                    fits:[e,...e.querySelectorAll('summary,li,.abyss-intel-wave')].every(item => {
                        const r=item.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && item.scrollWidth<=item.clientWidth;
                    }), waves:[...e.querySelectorAll('.abyss-intel-wave')].map(wave => {
                        const r=wave.getBoundingClientRect();return {x:r.x,y:r.y,bottom:r.bottom};
                    })})""")
                self.assertTrue(metrics["fits"])
                upper, lower = metrics["waves"]
                if width <= 680:
                    self.assertGreaterEqual(lower["y"], upper["bottom"])
                else:
                    self.assertEqual(upper["y"], lower["y"])
                    self.assertGreater(lower["x"], upper["x"])
                output = REVIEW
                output.mkdir(parents=True, exist_ok=True)
                panel.evaluate("e=>{const c=e.closest('.content');c.scrollTop+=e.getBoundingClientRect().top-c.getBoundingClientRect().top-10;}")
                self.page.screenshot(path=str(output / f"intel-{width}-{theme}.png"), animations='disabled')

    def test_enemy_details_change_with_scene_and_preserve_refresh_collapse_choice(self):
        self.open_options()
        panel = self.page.locator(".abyss-intel")
        self.assertTrue(panel.is_visible())
        self.assertTrue(panel.evaluate("e => e.open"))
        self.assertEqual(panel.locator(".abyss-enemy-name strong").all_inner_texts(), ["蜜丽恩", "安熙恩"])
        self.assertEqual(panel.locator(".abyss-enemy-tag").all_inner_texts(), ["火", "术师", "水", "术师"])
        self.assertIn("神秘气息", panel.inner_text())
        self.assertIn("境域坏灭", panel.inner_text())
        self.assertNotIn("StatusFun", panel.inner_text())
        count = self.requests.count("/api/abyss/options")
        self.select_scene(7)
        self.assertEqual(panel.locator(".abyss-enemy").first.get_attribute("data-enemy-id"), "FH027")
        self.assertEqual(self.requests.count("/api/abyss/options"), count)
        self.assertTrue(panel.locator('[data-skill-id="EP1EH027S1"] .missing').is_visible())
        panel.locator(".abyss-enemy > summary").first.click()
        self.page.locator(".abyss-options-refresh").click()
        self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loading === '0'")
        self.assertFalse(panel.locator(".abyss-enemy").first.evaluate("e=>e.open"))
        self.page.evaluate("activeAccount='second'; renderManualPanel([effectTask]); setAbyssCfg({sceneId:'MysteriousRealm_7'})")
        self.open_options()
        panel = self.page.locator(".abyss-intel")
        self.assertEqual(panel.locator(".abyss-enemy").first.get_attribute("data-enemy-id"), "FH038")
        self.assertEqual(panel.locator('[data-skill-id="H038S4"]').count(), 1)
        self.assertEqual(panel.locator('[data-skill-id="EP1EH027S1"]').count(), 0)

    def test_enemy_summons_missing_data_and_html_are_safe_and_failure_clears_old_details(self):
        self.responses["first"] = options("Fantasy_H144")
        first = self.responses["first"]["scenes"][0]["enemies"]["upper"][0]
        first["name"] = '<img src=x onerror=alert(1)>'
        first["skills"][0]["description"] = '<script>alert(1)</script>'
        self.open_options()
        panel = self.page.locator(".abyss-intel")
        self.assertIn(first["name"], panel.inner_text())
        self.assertIn('<script>alert(1)</script>', panel.inner_text())
        self.assertEqual(panel.locator('script,img[src=x]').count(), 0)
        self.select_scene(7)
        upper = panel.locator('.abyss-intel-wave').first
        self.assertEqual(upper.locator('.abyss-enemy').count(), 3)
        self.assertEqual(upper.locator('.abyss-enemy-name strong').all_inner_texts()[1:], ["金光星源", "金光星源"])
        self.assertEqual(upper.locator('.abyss-enemy-kind.boss').count(), 1)
        self.fail_options = True
        self.page.locator('.abyss-options-refresh').click()
        self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loading === '0'")
        self.assertTrue(panel.is_hidden())
        self.assertEqual(panel.locator('.abyss-enemy').count(), 0)

    def test_enemy_mobile_desktop_themes_and_whole_page_scroll(self):
        for width in (320, 390, 1280):
            self.page.set_viewport_size({"width": width, "height": 900})
            self.page.evaluate("setDrawer(false); renderManualPanel([effectTask]); setAbyssCfg({sceneId:'MysteriousRealm_1'})")
            self.open_options()
            panel = self.page.locator('.abyss-intel')
            for theme in ('dark', 'light'):
                self.page.evaluate("theme=>document.documentElement.dataset.theme=theme", theme)
                metrics = panel.evaluate("""e => ({fits:[e,...e.querySelectorAll('summary,li,.abyss-intel-wave')].every(node=>{
                    const r=node.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&node.scrollWidth<=node.clientWidth;
                }), waves:[...e.querySelectorAll('.abyss-intel-wave')].map(node=>{const r=node.getBoundingClientRect();return{x:r.x,y:r.y,bottom:r.bottom};}),
                scrollable:[e,...e.querySelectorAll('*')].some(node=>['auto','scroll'].includes(getComputedStyle(node).overflowY)&&node.scrollHeight>node.clientHeight+1)})""")
                self.assertTrue(metrics['fits'])
                self.assertFalse(metrics['scrollable'])
                upper, lower = metrics['waves']
                if width <= 680:
                    self.assertGreaterEqual(lower['y'], upper['bottom'])
                else:
                    self.assertEqual(upper['y'], lower['y'])
                    self.assertGreater(lower['x'], upper['x'])
                panel.evaluate("e=>{const c=e.closest('.content');c.scrollTop+=e.getBoundingClientRect().top-c.getBoundingClientRect().top-10;}")
                self.page.screenshot(path=str(REVIEW / f'enemies-{width}-{theme}.png'), animations='disabled')

    def test_picker_follows_current_stage_and_refreshes_previous_stars_without_options_requests(self):
        self.open_options()
        before=self.requests.count('/api/abyss/options')
        scenes=deepcopy(self.responses['first']['scenes'])
        scenes[0]['stars']=[1,1,1,1]
        progress={'running':True,'progress':{'sceneId':'MysteriousRealm_2','phase':'planning','message':'第2关',
            'scenes':scenes}}
        self.page.evaluate('value=>applyAbyssBattle("first",value)',progress)
        self.assertEqual(self.page.locator('.abyss-scene .dd-cur .abyss-scene-name').inner_text(),'第 2 关')
        self.assertEqual(self.page.locator('.abyss-scene .dd-opt[data-val="MysteriousRealm_1"] .earned').count(),4)
        self.assertEqual(self.page.locator('.abyss-intel-scene').inner_text(),'第 2 关')
        self.assertEqual(self.page.evaluate('getAbyssCfg().sceneId'),'MysteriousRealm_2')
        self.page.evaluate("window.picker = document.querySelector('.abyss-scene')")
        progress['progress']['phase']='battle';progress['progress']['round']=2
        self.page.evaluate('value=>applyAbyssBattle("first",value)',progress)
        self.assertTrue(self.page.evaluate("picker === document.querySelector('.abyss-scene')"))
        self.assertEqual(self.requests.count('/api/abyss/options'),before)
        self.page.evaluate('value=>applyAbyssBattle("second",value)',{**progress,'progress':{**progress['progress'],'sceneId':'MysteriousRealm_3'}})
        self.assertEqual(self.page.evaluate('getAbyssCfg().sceneId'),'MysteriousRealm_2')
        self.assertEqual(self.page.evaluate('getAbyssCfg("second").sceneId'),'MysteriousRealm_3')
        self.page.locator('.abyss-widget').screenshot(path=str(REVIEW/'live-stage-and-stars.png'))

    def test_empty_auto_teams_are_runtime_only_and_isolated_from_other_accounts(self):
        self.responses['first']['roles']=[{'roleId':f'r{i}','staticId':f'H{i+1:03}','name':f'角色{i+1}','level':60} for i in range(8)]
        self.open_options();self.hold_run=True
        self.page.locator('.abyss-auto-row').click()
        with self.page.expect_request('**/api/abyss/run'):
            self.page.locator('.abyss-start').click()
        self.page.wait_for_function('abyssRunning.first===true')
        self.assertTrue(self.runs[0]['autoAdvance']);self.assertEqual(self.runs[0]['waves'],{'upper':[None]*4,'lower':[None]*4})
        progress={'running':True,'progress':{'sceneId':'MysteriousRealm_2','phase':'equipping','message':'正在自动配队与临时配装','attempt':1,'attemptLimit':5,'autoConfigured':True,'waves':{'upper':[f'r{i}' for i in range(4)],'lower':[f'r{i}' for i in range(4,8)]}}}
        self.page.evaluate('value=>applyAbyssBattle("first",value)',progress)
        self.assertEqual(self.page.locator('.abyss-slot.filled').count(),8)
        self.assertEqual(self.page.locator('.abyss-slot:not(:disabled)').count(),0)
        self.assertEqual(self.page.evaluate('getAbyssCfg().upper'),[None]*4)
        self.assertIn('自动配队',self.page.locator('.abyss-auto-progress').inner_text())
        self.assertEqual(self.page.locator('.abyss-intel-scene').inner_text(),'第 2 关')
        for width in (320,390,1280):
            self.page.set_viewport_size({'width':width,'height':900});self.page.evaluate('setDrawer(false)')
            self.assertTrue(self.page.locator('.abyss-widget').evaluate("e=>[e,...e.querySelectorAll('.abyss-team-grid,.abyss-auto-row,.abyss-slot')].every(n=>n.scrollWidth<=n.clientWidth+1)"))
            self.page.locator('.abyss-team-grid').evaluate("e=>{const c=e.closest('.content');c.scrollTop+=e.getBoundingClientRect().top-c.getBoundingClientRect().top-10;}")
            self.page.screenshot(path=str(REVIEW/f'auto-team-{width}.png'),animations='disabled')
        self.page.evaluate("activeAccount='second';renderManualPanel([effectTask])");self.open_options()
        self.page.evaluate('value=>applyAbyssBattle("first",value)',progress)
        self.assertEqual(self.page.locator('.abyss-slot.filled').count(),0)
        self.run_pending.fulfill(content_type='application/json',body=json.dumps({'ok':True,'detail':'离线完成','result':{'recoveryRequired':False}}))
        self.page.wait_for_function('!abyssRunning.first')
        self.assertEqual(self.page.evaluate('getAbyssCfg().upper'),[None]*4)

    def test_empty_without_auto_or_half_a_team_cannot_start_manual_teams_still_work(self):
        self.open_options();self.page.locator('.abyss-start').click();self.assertEqual(self.runs,[])
        self.page.evaluate("setAbyssCfg({upper:['r0',null,null,null],lower:[null,null,null,null],autoAdvance:true})")
        self.page.locator('.abyss-start').click();self.assertEqual(self.runs,[])
        self.page.evaluate("setAbyssCfg({upper:['r0',null,null,null],lower:['r1',null,null,null],autoAdvance:true})")
        self.page.locator('.abyss-start').click();self.page.wait_for_function('!abyssRunning.first')
        self.assertEqual(self.runs[0]['waves']['upper'][0],'r0');self.assertEqual(self.runs[0]['waves']['lower'][0],'r1')

    def test_old_recovery_record_has_no_button_and_does_not_block_start(self):
        self.responses['first']['recoveryPending']=True
        self.responses['first']['config']={'sceneId':'MysteriousRealm_1','waves':{'upper':['old1'],'lower':['old2']}}
        self.page.evaluate("setAbyssCfg({upper:[null,null,null,null],lower:[null,null,null,null],autoAdvance:true})")
        self.open_options();self.assertEqual(self.page.locator('.abyss-slot.filled').count(),0)
        self.assertIn('如果未选择角色，开启自动推关时，将自动配置角色和装备',self.page.locator('.abyss-auto-row').inner_text())
        self.assertEqual(self.page.locator('.abyss-restore').count(),0)
        self.assertTrue(self.page.locator('.abyss-start').is_enabled())
        self.run_response={'ok':True,'detail':'本次挑战完成','result':{'recoveryRequired':False,'recoveryWarning':'上次装备未完全恢复。已跳过旧记录，继续本次挑战。'}}
        self.page.locator('.abyss-start').click();self.page.wait_for_function('!abyssRunning.first')
        self.assertEqual(len(self.runs),1);self.assertEqual(self.restore_calls,0)
        self.assertIn('上次装备未完全恢复',self.page.locator('.abyss-summary').inner_text())
        self.assertTrue(self.page.locator('.abyss-start').is_enabled())

    def test_server_errors_are_visible_below_buttons_and_survive_refresh_and_account_switch(self):
        message='自动推关已停止：第 2 关，角色背包已满（115/115），请在游戏中清理空间'
        self.run_response={'ok':False,'detail':message,'result':{'recoveryRequired':False}}
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.evaluate('setDrawer(false)')
        self.open_options();self.page.locator('.abyss-auto-row').click()
        self.page.locator('.abyss-start').click();self.page.wait_for_function('!abyssRunning.first')
        notice=self.page.locator('.abyss-summary')
        self.assertEqual(notice.inner_text(),message)
        self.assertEqual(notice.get_attribute('data-tone'),'error')
        self.assertEqual(notice.get_attribute('role'),'alert')
        self.assertTrue(notice.evaluate("e=>e.getBoundingClientRect().top>=e.previousElementSibling.getBoundingClientRect().bottom"))
        self.assertTrue(notice.evaluate('e=>e.scrollWidth<=e.clientWidth'))
        self.page.locator('.abyss-options-refresh').click();self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loading==='0'")
        self.assertEqual(notice.inner_text(),message)
        notice.evaluate("e=>e.scrollIntoView({block:'center'})")
        self.page.screenshot(path=str(REVIEW/'error-below-actions-390.png'),animations='disabled')
        self.page.evaluate("activeAccount='second';renderManualPanel([effectTask])");self.open_options()
        self.assertEqual(self.page.locator('.abyss-summary').inner_text(),'')
        self.page.evaluate("activeAccount='first';renderManualPanel([effectTask])");self.open_options()
        self.assertEqual(self.page.locator('.abyss-summary').inner_text(),message)
        self.run_response={'ok':True,'detail':'自动推关完成','result':{'recoveryRequired':False}}
        self.page.locator('.abyss-start').click();self.page.wait_for_function('!abyssRunning.first')
        self.assertEqual(self.page.locator('.abyss-summary').get_attribute('data-tone'),'success')
        self.assertEqual(self.page.locator('.abyss-summary').inner_text(),'自动推关完成')

    def test_http_error_keeps_specific_server_reason_in_bottom_notice(self):
        self.run_status=400;self.run_response={'detail':'战斗连接中断，正在核对服务器结算 <失败原因>'}
        self.open_options();self.page.locator('.abyss-auto-row').click()
        self.page.locator('.abyss-start').click();self.page.wait_for_function('!abyssRunning.first')
        notice=self.page.locator('.abyss-summary')
        self.assertIn(self.run_response['detail'],notice.inner_text())
        self.assertEqual(notice.get_attribute('data-tone'),'error')
        self.assertEqual(notice.locator('*').count(),0)


if __name__ == "__main__":
    unittest.main()
