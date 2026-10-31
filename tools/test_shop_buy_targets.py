"""Offline coverage for per-slot secret-shop purchases and account preferences."""
import copy
import json
import mimetypes
from pathlib import Path
import unittest
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

from backend.game_client import GameMessage
from backend.tasks import BUY_TARGETS, DEFAULT_SHOP_WANTED, shop_reset, task_shop_free


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://shop-targets.invalid"
FREE = {"id": "shop_free", "name": "秘密商店刷新(免费)", "desc": "免费刷新", "wantsBuyTargets": True}
DIAMOND = {"id": "shop_diamond", "name": "秘密商店刷新(钻石)", "desc": "钻石刷新", "ui": "shop"}


class ShopClient:
    def __init__(self, cooldown=False):
        self.cooldown = cooldown
        self.purchases = []
        self.account_state = {"StoreRecordContainer": {"Records": self.shelf("old")}}

    @staticmethod
    def shelf(prefix):
        return [{"Store": "SecretShop", "StaticID": f"{prefix}-{sid}", "BuyCount": 0,
                 "DropResult": {"Items": [{"Item": {"StaticID": sid, "Count": 1}}]}}
                for sid in DEFAULT_SHOP_WANTED]

    def _auth_data(self, data):
        return data

    def call(self, route, data):
        if route == "StoreHandler.ResetRandomStore":
            if self.cooldown:
                raise GameMessage("CantReset")
            return {"Records": self.shelf("new")}
        assert route == "StoreHandler.BuyCommodity", route
        item = data["Record"]["DropResult"]["Items"][0]["Item"]
        self.purchases.append(item["StaticID"])
        return {"Drop": {"Items": [{"Item": copy.deepcopy(item)}]}}


class ShopPurchaseTests(unittest.TestCase):
    def test_free_shop_buys_only_selected_slots_before_and_after_refresh(self):
        client = ShopClient()
        result = task_shop_free(client, {"wanted": ["5", "EC11", "EC31"]})
        self.assertTrue(result["ok"])
        self.assertEqual(client.purchases, ["5", "EC11", "EC31"] * 2)

    def test_empty_selection_never_falls_back_to_buying_everything(self):
        for cooldown in (False, True):
            with self.subTest(cooldown=cooldown):
                client = ShopClient(cooldown)
                self.assertTrue(task_shop_free(client, {"wanted": []})["ok"])
                self.assertEqual(client.purchases, [])

    def test_unconfigured_accounts_keep_all_existing_targets(self):
        client = ShopClient()
        task_shop_free(client)
        self.assertEqual(client.purchases, DEFAULT_SHOP_WANTED * 2)

    def test_diamond_shop_buys_only_selected_slots(self):
        client = ShopClient()
        self.assertTrue(shop_reset(client, True, ["EC21", "EC51"])["ok"])
        self.assertEqual(client.purchases, ["EC21", "EC51"])


class ShopBuyTargetsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 900}, has_touch=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors, self.unexpected, self.requests = [], [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.serve)
        self.page.goto(ORIGIN)
        self.page.wait_for_function("typeof enterApp === 'function' && updateStartupReady")
        self.page.evaluate("""async () => {
            for (const account of ['first', 'second']) rememberAccount({account, status:{name:account}});
            activeAccount='first'; enterApp();
            await loadTasks(); showSection('auto');
        }""")

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])

    def serve(self, route):
        url = urlsplit(route.request.url)
        path = unquote(url.path)
        body = route.request.post_data_json if route.request.method == "POST" else None
        self.requests.append((path, body))
        if url.netloc != urlsplit(ORIGIN).netloc:
            self.unexpected.append(route.request.url)
            route.abort()
            return
        if path.startswith("/api/"):
            responses = {
                "/api/accounts": {"accounts": []}, "/api/preferences": {"settings": {}},
                "/api/config": {"platform": "windows", "version": "test"},
                "/api/tasks": {"toggle": [FREE], "manual": [DIAMOND], "buyTargets": BUY_TARGETS},
                "/api/update/status": {"testServer": True, "available": False},
                "/api/update/check": {"testServer": True, "available": False},
                "/api/resources/status": {"phase": "ready"},
                "/api/logs/auto": {"entries": [], "logSeq": 0},
                "/api/toggles": {"ok": True},
                "/api/shop/reset": {"ok": True, "bought": {}, "legendaryMatches": []},
                "/api/status": {"status": {}, "nextClaims": {}},
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

    def open_free(self):
        root = self.page.locator("#auto-shop_free")
        root.locator(".shop-free-toggle").click()
        root.locator(".shop-chip-toggle").click()
        return root

    @staticmethod
    def part(root, sid):
        return root.locator(f'label:has(> input[data-shop-part="{sid}"])')

    def test_free_slot_selection_survives_other_changes_and_is_sent_exactly(self):
        root = self.open_free()
        for sid in ("EC41", "EC51", "EC61"):
            self.part(root, sid).click()
        self.assertTrue(root.locator('[data-shop-target="chip"]').evaluate("e => e.indeterminate"))
        self.assertEqual(root.locator(".shop-chip-summary").inner_text(), "下级 · 武器、头盔、铠甲")
        root.locator('label:has(> input[data-shop-target="mystery"])').click()
        expected = ["5", "EC11", "EC21", "EC31"]
        self.assertEqual(self.page.evaluate("wantedIds()"), expected)
        self.page.wait_for_function("wantedIds().length === 4")
        self.page.evaluate("renderAutoPanel(TOGGLE_TASKS)")
        self.assertTrue(root.locator(".shop-chip-picker").is_visible())
        self.assertEqual(root.locator("[data-shop-part]:checked").count(), 3)
        self.assertEqual(self.page.evaluate("toggleParams(['shop_free']).shop_free.wanted"), expected)
        self.assertTrue(any(body and body.get("params", {}).get("shop_free", {}).get("wanted") == expected
                            for path, body in self.requests if path == "/api/toggles"))

    def test_master_checkbox_all_none_and_all_button(self):
        root = self.open_free()
        root.locator(".shop-chip-master").click()
        self.assertEqual(root.locator("[data-shop-part]:checked").count(), 0)
        self.assertIn("不购买芯片", root.locator(".shop-chip-summary").inner_text())
        self.part(root, "EC51").click()
        self.assertEqual(root.locator("[data-shop-part]:checked").count(), 1)
        root.locator(".shop-chip-master").click()
        self.assertEqual(root.locator("[data-shop-part]:checked").count(), 6)
        root.locator(".shop-chip-master").click()
        root.locator(".shop-chip-all").click()
        self.assertEqual(root.locator("[data-shop-part]:checked").count(), 6)

    def test_accounts_restore_their_own_selection_and_expansion(self):
        root = self.open_free()
        self.part(root, "EC11").click()
        self.page.evaluate("activeAccount='second'; renderAutoPanel(TOGGLE_TASKS)")
        self.assertFalse(root.locator(".shop-chip-picker").is_visible())
        self.assertEqual(root.locator("[data-shop-part]:checked").count(), 6)
        self.page.evaluate("activeAccount='first'; renderAutoPanel(TOGGLE_TASKS)")
        self.assertTrue(root.locator(".shop-chip-picker").is_visible())
        self.assertEqual(root.locator("[data-shop-part]:checked").count(), 5)
        self.assertFalse(root.locator('[data-shop-part="EC11"]').is_checked())

    def test_diamond_parts_expansion_is_scoped_to_account(self):
        self.page.evaluate("""() => { renderManualPanel(TASKS); showSection('manual'); }""")
        root = self.page.locator("#shop-shop_diamond")
        root.locator(".shop-toggle").click()
        root.locator(".shop-chip-toggle").click()
        self.assertTrue(root.locator(".shop-chip-picker").is_visible())
        self.page.evaluate("activeAccount='second'; renderManualPanel(TASKS)")
        self.assertFalse(root.locator(".shop-chip-picker").is_visible())
        self.page.evaluate("activeAccount='first'; renderManualPanel(TASKS)")
        self.assertTrue(root.locator(".shop-chip-picker").is_visible())

    def test_diamond_legacy_migration_partial_selection_and_purchase_payload(self):
        self.page.evaluate("""() => {
            frontendStorageSet(shopLsKey(), JSON.stringify({wanted:['recruit','chip'], count:1}));
            renderManualPanel(TASKS); showSection('manual');
        }""")
        root = self.page.locator("#shop-shop_diamond")
        root.locator(".shop-toggle").click()
        root.locator(".shop-chip-toggle").click()
        self.assertEqual(root.locator("[data-shop-part]:checked").count(), 6)
        self.assertFalse(root.locator('[data-shop-target="mystery"]').is_checked())
        for sid in ("EC21", "EC31", "EC41", "EC61"):
            self.part(root, sid).click()
        self.page.evaluate("renderManualPanel(TASKS)")
        self.assertTrue(root.locator(".shop-chip-picker").is_visible())
        self.assertEqual(root.locator("[data-shop-part]:checked").count(), 2)
        self.assertEqual(self.page.evaluate("shopWantedIds()"), ["5", "EC11", "EC51"])
        root.locator(".shop-start").click()
        self.page.wait_for_function("!shopRunning.first")
        purchases = [body for path, body in self.requests if path == "/api/shop/reset"]
        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0]["autoBuy"], ["5", "EC11", "EC51"])
        self.assertEqual(purchases[0]["account"], "first")
        self.page.evaluate("activeAccount='second'; renderManualPanel(TASKS)")
        self.assertEqual(self.page.evaluate("shopWantedIds()"), DEFAULT_SHOP_WANTED)

    def test_empty_legacy_and_new_preferences_stay_empty(self):
        for cfg in ({"wanted": []}, {"wantedIds": []}):
            self.page.evaluate("cfg => frontendStorageSet(shopLsKey(), JSON.stringify(cfg))", cfg)
            self.assertEqual(self.page.evaluate("shopWantedIds()"), [])
        self.page.evaluate("ACCOUNTS.first.params={shop_free:{wanted:[]}}; renderAutoPanel(TOGGLE_TASKS)")
        self.assertEqual(self.page.evaluate("wantedIds()"), [])
        self.assertEqual(self.page.locator("#auto-shop_free [data-shop-part]:checked").count(), 0)

    def test_mobile_parts_fit_both_themes_and_shop_panels(self):
        for width in (320, 390, 1280):
            self.page.set_viewport_size({"width": width, "height": 900})
            for theme in ("dark", "light"):
                self.page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                for section, root_id in (("auto", "auto-shop_free"), ("manual", "shop-shop_diamond")):
                    self.page.evaluate("section => showSection(section)", section)
                    root = self.page.locator(f"#{root_id}")
                    if not root.locator(".shop-buy-targets").is_visible():
                        root.locator(".shop-head > .mini, .auto-head > .mini").click()
                    if not root.locator(".shop-chip-picker").is_visible():
                        root.locator(".shop-chip-toggle").click()
                    self.assertTrue(root.locator(".shop-chip-grid").evaluate("""grid => {
                        const r=grid.getBoundingClientRect();
                        return grid.scrollWidth <= grid.clientWidth && [...grid.children].every(item => {
                            const b=item.getBoundingClientRect();
                            return b.left >= r.left-1 && b.right <= r.right+1 && item.scrollWidth <= item.clientWidth;
                        });
                    }"""), (width, theme, section))


if __name__ == "__main__":
    unittest.main()
