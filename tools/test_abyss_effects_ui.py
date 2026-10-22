"""Abyss effect presentation with production frontend and offline game data."""
import json
import mimetypes
from copy import deepcopy
from pathlib import Path
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.sync_api import sync_playwright

from backend.tasks import abyss_scenes, abyss_scene_effects

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://abyss-effects.invalid"
TASK = {"id": "abyss_auto", "name": "虚构深渊", "desc": "配置队伍，自动进行战斗", "ui": "abyss"}


def options(activity):
    state = {"SceneDataContainer": {"Scenes": [
        {"StaticID": f"MysteriousRealm_{index}", "Stars": [1, 1, 0, 0]} for index in range(1, 11)]},
        "ActivityDBSettingContainer": {"SettingList": [
            {"ActivityID": sid, "IsIgnoreTime": True}
            for sid in ("Version_Fantasy", "Version_Fantasy01_1-6", activity)]}}
    return {"scenes": [{**scene, "effects": abyss_scene_effects(state, scene["id"])}
                       for scene in abyss_scenes(state)], "roles": []}


class AbyssEffectsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.payloads = {"first": options("Fantasy_H189"), "second": options("Fantasy_H610")}

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors, self.unexpected, self.requests = [], [], []
        self.responses = deepcopy(self.payloads)
        self.hold_account, self.pending = None, None
        self.fail_options = False
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.serve)
        self.page.goto(ORIGIN)
        self.page.wait_for_function("typeof enterApp === 'function' && updateStartupReady")
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
        if path.startswith("/api/"):
            responses = {
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
            else ROOT / path.lstrip("/"))
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

    def test_selection_refresh_and_account_isolation(self):
        self.open_options()
        panel = self.page.locator(".abyss-effects")
        self.assertTrue(panel.evaluate("e => e.open"))
        self.assertIn("狙击", panel.locator(".bonuses").first.inner_text())
        self.assertEqual(panel.locator(".abyss-effect-wave h4").all_inner_texts(), ["上半", "下半"])
        count = self.requests.count("/api/abyss/options")
        self.select_scene(7)
        self.assertEqual(panel.locator(".abyss-effects-scene").inner_text(), "第 7 关")
        self.assertIn("不屈", panel.locator(".bonuses").first.inner_text())
        self.assertIn("术师", panel.locator(".restrictions").first.inner_text())
        self.assertEqual(self.requests.count("/api/abyss/options"), count)
        panel.locator("summary").click()
        self.page.locator(".abyss-options-refresh").click()
        self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loading === '0'")
        self.assertFalse(panel.evaluate("e => e.open"), "Refreshing must preserve the user's collapse choice")
        self.hold_account = "first"
        self.page.locator(".abyss-options-refresh").click()
        self.page.wait_for_timeout(40)
        self.assertIsNotNone(self.pending)
        self.page.evaluate("activeAccount='second'; renderManualPanel([effectTask]); setAbyssCfg({sceneId:'MysteriousRealm_7'})")
        self.open_options()
        self.assertIn("护盾", self.page.locator(".abyss-effects .bonuses").first.inner_text())
        self.pending.fulfill(content_type="application/json", body=json.dumps(self.responses["first"]))
        self.assertIn("护盾", self.page.locator(".abyss-effects .bonuses").first.inner_text())
        self.assertNotIn("不屈", self.page.locator(".abyss-effects").inner_text())

    def test_missing_empty_and_failed_effects_never_leave_stale_text(self):
        first = self.responses["first"]["scenes"][0]
        first["effects"]["upper"]["bonuses"] = [
            {"id": "FG_Special_021", "description": "效果说明暂缺", "descriptionMissing": True},
            {"id": "literal", "description": "<img src=x onerror=alert(1)>", "descriptionMissing": False}]
        first["effects"]["lower"] = {"bonuses": [], "restrictions": []}
        self.open_options()
        panel = self.page.locator(".abyss-effects")
        self.assertEqual(panel.locator(".abyss-effect-missing").inner_text(), "效果说明暂缺")
        self.assertIn("FG_Special_021", panel.locator(".abyss-effect-missing").get_attribute("title"))
        self.assertIn("<img src=x onerror=alert(1)>", panel.inner_text())
        self.assertEqual(panel.locator("img").count(), 0)
        self.assertEqual(panel.locator(".abyss-effect-empty").all_inner_texts(), ["无", "无"])
        self.fail_options = True
        self.page.locator(".abyss-options-refresh").click()
        self.page.wait_for_function("document.querySelector('.abyss-widget').dataset.loading === '0'")
        self.assertTrue(panel.is_hidden())
        self.assertEqual(panel.locator(".abyss-effects-content").inner_html(), "")
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
            panel = self.page.locator(".abyss-effects")
            self.assertEqual(panel.evaluate("e => e.open"), width > 680)
            if width <= 680:
                panel.locator("summary").click()
            for theme in ("dark", "light"):
                self.page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                metrics = panel.evaluate("""e => ({
                    fits:[e,...e.querySelectorAll('summary,li,.abyss-effect-wave')].every(item => {
                        const r=item.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && item.scrollWidth<=item.clientWidth;
                    }), waves:[...e.querySelectorAll('.abyss-effect-wave')].map(wave => {
                        const r=wave.getBoundingClientRect();return {x:r.x,y:r.y,bottom:r.bottom};
                    })})""")
                self.assertTrue(metrics["fits"])
                upper, lower = metrics["waves"]
                if width <= 680:
                    self.assertGreaterEqual(lower["y"], upper["bottom"])
                else:
                    self.assertEqual(upper["y"], lower["y"])
                    self.assertGreater(lower["x"], upper["x"])
                self.page.locator(".abyss-widget").screenshot(path=str(ROOT / "previews" / f"abyss-effects-{width}-{theme}.png"))


if __name__ == "__main__":
    unittest.main()
