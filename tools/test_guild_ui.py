"""Guild page checks using real frontend files and offline API fixtures."""
import json
import mimetypes
from pathlib import Path
import unittest
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://guild-ui.invalid"


def weekly_summary():
    catalog = json.loads((ROOT / 'assets/guild_quests.json').read_text(encoding='utf-8'))
    return {"week": 666, "personalKnown": True, "score": 1200, "personalClaimed": 1, "tiersClaimed": 1,
            "personal": [
                {"id": "GuildCheckIn", "name": "出席佣兵团", "count": 3, "target": 5, "state": "progress"},
                {"id": "GuildDonatedCourage", "name": "捐赠英勇证明", "count": 12, "target": 10, "state": "ready"},
                {"id": "GuildGetRing", "name": "获得戒指", "count": 10, "target": 10, "state": "claimed"}],
            "tiers": [{"id": key, **row, "state": "claimed" if i == 0 else "ready" if i == 1 else "progress"}
                      for i, (key, row) in enumerate(catalog["tiers"].items())]}


def store_summary():
    catalog = json.loads((ROOT / 'assets/guild_store.json').read_text(encoding='utf-8'))['goods']
    return {'currencies': {'29': 3456, '30': 2345}, 'running': None, 'goods': [
        {'id': key, **row, 'available': True, 'error': None, 'blocked': False, 'bought': 0,
         'remaining': row['limit'], 'buyable': min(row['limit'] or 10, 10)} for key, row in catalog.items()]}


def captain_store_summary(access=None):
    return {'access': access or {'allowed': True, 'authority': 1, 'reason': None},
            'currencies': {'80': 1200, '81': 240}, 'running': None,
            'goods': [
                {'id': 'GuildMoneyBonus', 'name': '富豪的祝福', 'currency': '80', 'price': 200000, 'kind': 'buff',
                 'eligible': True, 'available': True, 'remainingMs': 0, 'remaining': None,
                 'reset': None, 'blocked': False, 'error': None},
                {'id': 'GuildHeroExp', 'name': '知识的祝福', 'currency': '80', 'price': 200000, 'kind': 'buff',
                 'eligible': True, 'available': True, 'remainingMs': 0, 'remaining': None,
                 'reset': None, 'blocked': False, 'error': None},
                {'id': 'GuildEnergy30', 'name': '30 个能源', 'currency': '80', 'price': 1000000, 'kind': 'limited',
                 'eligible': True, 'available': True, 'remainingMs': None, 'remaining': 1,
                 'reset': 'Day', 'blocked': False, 'error': None},
                {'id': 'GuildEnergy50', 'name': '50 个能源', 'currency': '81', 'price': 30, 'kind': 'limited',
                 'eligible': True, 'available': True, 'remainingMs': None, 'remaining': 1,
                 'reset': 'Day', 'blocked': False, 'error': None},
                {'id': 'GuildBox', 'name': '佣兵团补给箱', 'currency': '81', 'price': 300, 'kind': 'limited',
                 'eligible': True, 'available': True, 'remainingMs': None, 'remaining': 1,
                 'reset': 'Week', 'blocked': False, 'error': None},
            ]}


def summary(name="星陨佣兵团", revision=2, **fields):
    return {"joined": True, "name": name, "level": 18, "memberCount": 30,
            "icon": "/assets/guild/UI80_Guild_Sign_11.png", "frame": "/assets/guild/GuildFrame0202.png",
            "checkedIn": True, "canClaim": True, "updatedAt": 1792100000000,
            "loading": False, "claiming": False, "claimError": None,
            "donating": None, "donationErrors": {},
            "aid": {"requested": False, "requestKnown": True, "reward": 80,
                    "limit": 200, "running": None, "error": None},
            "weekly": weekly_summary(),
            "store": store_summary(),
            "captainStore": captain_store_summary(),
            "donations": {"gold": {"donated": 0, "limit": 10, "count": 10},
                          "courage": {"donated": 0, "limit": 3, "count": 3}},
            "error": None, "loginRevision": 1, "revision": revision, **fields}


class GuildUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

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
        self.hold_overview = False
        self.pending_overview = None
        self.toggle_error = False
        self.overview_error = False
        self.score_jobs = {}
        self.score_start_error = False
        self.score_status_error = False
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.serve)
        self.page.goto(ORIGIN)
        self.page.wait_for_function("typeof enterApp === 'function' && updateStartupReady")
        self.page.evaluate("""guild => {
            enterApp();
            ACCOUNTS.first = {status:{}, nextClaims:{}, params:{}, toggles:{}, guild, stateRevision:1};
            ACCOUNTS.second = {status:{}, nextClaims:{}, params:{}, toggles:{},
              guild:{joined:false, loginRevision:1, revision:0}, stateRevision:1};
            activeAccount = 'first';
            window.dispatchEvent(new CustomEvent('lucima:account-change'));
        }""", summary())

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
            if path == '/api/guild/quests/start':
                job = {'id': body['requestId'], 'mode': body['mode'], 'kind': 'quest_rewards',
                       'state': 'running', 'running': True, 'score': 1200, 'target': 2800, 'completed': 0}
                self.score_jobs[body['account']] = job
                route.fulfill(status=500 if self.score_start_error else 200, content_type='application/json',
                              body=json.dumps({'detail': '响应丢失'} if self.score_start_error else job))
                return
            if path == '/api/guild/quests/cancel':
                job = self.score_jobs[body['account']]
                job.update(state='cancelled', running=False)
                route.fulfill(content_type='application/json', body=json.dumps(job))
                return
            if path == '/api/guild/quests':
                from urllib.parse import parse_qs
                account = parse_qs(url.query).get('account', [''])[0]
                job = self.score_jobs.get(account, {'state': 'idle', 'running': False})
                # Production status includes the current guild cache as well as the job.
                guild = job.get('guild') or summary(weekly={**weekly_summary(), 'score': job.get('score', 1200)})
                route.fulfill(status=500 if self.score_status_error else 200, content_type='application/json',
                              body=json.dumps({'detail': '读取失败'} if self.score_status_error else {**job, 'guild': guild}))
                return
            if path == "/api/guild/overview" and self.hold_overview:
                self.pending_overview = route
                return
            if (path == "/api/toggles" and self.toggle_error) or (path == "/api/guild/overview" and self.overview_error):
                route.fulfill(status=400, content_type="application/json", body=json.dumps({"detail": "测试失败"}))
                return
            responses = {
                "/api/accounts": {"accounts": []}, "/api/preferences": {"settings": {}},
                "/api/config": {"platform": "windows", "version": "test"},
                "/api/tasks": {"toggle": [], "manual": [], "buyTargets": {}},
                "/api/update/status": {"testServer": True, "available": False},
                "/api/update/check": {"testServer": True, "available": False},
                "/api/logs/auto": {"entries": [], "logSeq": 0},
                "/api/guild/overview": {"guild": summary("刷新后的佣兵团", 4)},
                "/api/guild/store/buy": {"guild": summary(revision=10)},
                "/api/toggles": {"ok": True},
                "/api/status": {"status": {}},
                "/api/roster": {"roles": [], "items": []},
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

    def enter_guild(self):
        self.page.evaluate("showSection('guild')")
        self.page.locator("#guildName").wait_for(state="visible")

    def count(self, path):
        return sum(row[0] == path for row in self.requests)

    def test_cached_summary_no_extra_entry_requests_and_other_features_absent(self):
        self.enter_guild()
        self.assertEqual(self.page.locator("#guildName").inner_text(), "星陨佣兵团")
        self.assertEqual(self.page.locator("#guildLevel").inner_text(), "18")
        self.assertEqual(self.page.locator("#guildMembers").inner_text(), "30")
        self.page.evaluate("showSection('info'); showSection('guild')")
        self.assertEqual(self.count("/api/guild/overview"), 0)
        self.assertEqual(self.page.locator("#guildQuestPanel, .guild-buy, #guildCheckin, #guildAttendance").count(), 0)
        self.assertFalse(self.page.locator("#guildAutoSignin").is_checked())
        self.assertEqual(self.page.locator(".guild-heading, #guildAccount").count(), 0)
        self.assertEqual(self.page.locator("#guildOverview").count(), 0)
        self.assertEqual(self.page.locator(".guild-signin-row .tinfo #guildSigninState").count(), 1)
        self.page.wait_for_function("document.getElementById('guildIcon').naturalWidth > 0 && document.getElementById('guildFrame').naturalWidth > 0")
        self.assertTrue(self.page.locator("#guildAvatar").is_visible())

    def test_missing_avatar_layer_and_account_change_leave_no_broken_image(self):
        self.enter_guild()
        self.page.wait_for_function("document.getElementById('guildIcon').naturalWidth > 0")
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild, frame:null, revision:3})")
        self.assertFalse(self.page.locator("#guildFrame").is_visible())
        self.assertTrue(self.page.locator("#guildIcon").is_visible())
        self.page.evaluate("ACCOUNTS.second.toggles.guild_auto_signin=true; activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertFalse(self.page.locator("#guildAvatar").is_visible())
        self.assertIsNone(self.page.locator("#guildIcon").get_attribute("src"))
        self.assertEqual(self.page.locator(".guild-info-card").inner_text(), "当前尚未加入佣兵团")
        self.assertFalse(self.page.locator("#guildRefresh").is_visible())
        self.assertTrue(self.page.locator("#guildRefresh").is_disabled())
        self.assertTrue(self.page.locator("#guildAutoSignin").is_checked())
        self.assertTrue(self.page.locator("#guildAutoSignin").is_disabled())
        self.assertEqual(self.page.locator("#guildSigninState").inner_text(), "未加入佣兵团，暂不执行")
        self.assertEqual(self.count("/api/guild/overview"), 0)
        self.assertEqual(self.count("/api/toggles"), 0)
        self.assertTrue(self.page.locator("#guildAutoDonate").is_disabled())
        self.assertTrue(self.page.locator("#guildDonateOptions").is_disabled())

    def test_refresh_only_svg_spins_and_updates_summary(self):
        self.enter_guild()
        self.page.wait_for_function("document.getElementById('guildIcon').naturalWidth > 0")
        height = self.page.locator(".guild-info-card").bounding_box()["height"]
        self.hold_overview = True
        self.page.locator("#guildRefresh").click()
        self.page.wait_for_function("document.getElementById('guildRefresh').classList.contains('loading')")
        styles = self.page.locator("#guildRefresh").evaluate("e => [getComputedStyle(e).animationName, getComputedStyle(e.querySelector('svg')).animationName]")
        self.assertEqual(styles[0], "none")
        self.assertNotEqual(styles[1], "none")
        self.assertAlmostEqual(self.page.locator(".guild-info-card").bounding_box()["height"], height, delta=0.5)
        self.pending_overview.fulfill(content_type="application/json", body=json.dumps({"guild": summary("新佣兵团", 4)}))
        self.page.wait_for_function("document.getElementById('guildName').textContent === '新佣兵团'")
        self.assertEqual(self.count("/api/guild/overview"), 1)
        self.assertFalse(self.page.locator("#guildRefresh").is_disabled())
        self.hold_overview = False
        self.overview_error = True
        self.page.locator("#guildRefresh").click()
        self.page.wait_for_function("(ACCOUNTS.first.logHtml || '').includes('刷新失败')")
        self.assertEqual(self.page.locator("#guildName").inner_text(), "新佣兵团")
        self.assertAlmostEqual(self.page.locator(".guild-info-card").bounding_box()["height"], height, delta=0.5)
        self.assertEqual(self.page.locator("#guildOverview").count(), 0)
        self.assertFalse(self.page.locator("#guildRefresh").is_disabled())

    def test_toggle_saves_for_selected_account_and_failure_recovers(self):
        self.enter_guild()
        self.toggle_error = True
        self.page.locator(".guild-signin-row").click()
        self.page.wait_for_function("(ACCOUNTS.first.logHtml || '').includes('自动签到设置保存失败')")
        self.assertFalse(self.page.locator("#guildAutoSignin").is_checked())
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild, revision:3})")
        self.assertEqual(self.page.locator("#guildOverview").count(), 0)
        self.toggle_error = False
        self.page.locator(".guild-signin-row").click()
        self.page.wait_for_function("!document.getElementById('guildAutoSignin').disabled")
        self.assertTrue(self.page.locator("#guildAutoSignin").is_checked())
        self.assertEqual(self.page.locator("#guildOverview").count(), 0)
        body = [body for path, body in self.requests if path == "/api/toggles"][-1]
        self.assertEqual(body["account"], "first")
        self.assertTrue(body["toggles"]["guild_auto_signin"])

    def test_toggle_claim_progress_and_error_do_not_refresh_overview(self):
        self.enter_guild()
        self.page.locator(".guild-signin-row").click()
        self.page.wait_for_function("!document.getElementById('guildAutoSignin').disabled")
        self.assertEqual(self.count("/api/toggles"), 1)
        self.assertEqual(self.count("/api/guild/overview"), 0)
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild, claiming:true, revision:3})")
        self.assertEqual(self.page.locator("#guildSigninState").inner_text(), "正在领取日奖励…")
        self.assertNotIn("loading", self.page.locator("#guildRefresh").get_attribute("class"))
        self.assertEqual(self.page.locator("#guildRefresh").get_attribute("aria-busy"), "false")
        self.assertEqual(self.page.locator("#guildOverview").count(), 0)
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild, claiming:false, canClaim:false, revision:4})")
        self.assertEqual(self.page.locator("#guildSigninState").inner_text(), "今日已报到，暂无可领取的日奖励")
        self.assertFalse(self.page.locator("#guildRefresh").is_disabled())
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild, canClaim:true, claimError:'timeout', revision:5})")
        self.assertEqual(self.page.locator("#guildSigninState").inner_text(), "领取未确认，等待下次登录或手动刷新")
        self.assertIn("error", self.page.locator("#guildSigninState").get_attribute("class"))
        self.assertEqual(self.page.locator("#guildOverview").count(), 0)
        self.assertEqual(self.count("/api/guild/overview"), 0)

    def test_late_response_keeps_accounts_isolated_and_ignores_old_revision(self):
        self.enter_guild()
        self.hold_overview = True
        self.page.locator("#guildRefresh").click()
        self.page.wait_for_function("document.getElementById('guildRefresh').disabled")
        self.page.evaluate("activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.pending_overview.fulfill(content_type="application/json", body=json.dumps({"guild": summary("First only", 4)}))
        self.page.wait_for_function("ACCOUNTS.first.guild.revision===4")
        self.assertEqual(self.page.locator("#guildName").inner_text(), "当前尚未加入佣兵团")
        self.assertTrue(self.page.locator("#guildAutoSignin").is_disabled())
        self.assertFalse(self.page.locator("#guildStats").is_visible())
        self.page.evaluate("""() => {
          activeAccount='first'; window.dispatchEvent(new CustomEvent('lucima:account-change'));
          applyGuildSnapshot('first', {...ACCOUNTS.first.guild, name:'Stale', revision:1});
        }""")
        self.assertEqual(self.page.locator("#guildName").inner_text(), "First only")

    def test_donation_options_save_without_queries_and_recover_after_failure(self):
        self.enter_guild()
        self.assertFalse(self.page.locator("#guildAutoDonate").is_checked())
        self.assertEqual(self.page.locator("#guildDonateState").inner_text(), "金币 0/50000 · 英勇证明 0/3")
        self.page.locator("#guildDonateOptions").click()
        self.assertEqual(self.page.locator("#guildDonateOptions").get_attribute("aria-expanded"), "true")
        self.assertEqual(self.count("/api/toggles"), 0)
        choices = self.page.locator("#guildDonateChoices .bt-opt")
        self.assertTrue(choices.nth(0).locator("input").is_checked())
        self.assertTrue(choices.nth(1).locator("input").is_checked())
        choices.nth(1).click()
        self.page.wait_for_function("!document.getElementById('guildAutoDonate').disabled")
        self.page.locator('label[for="guildAutoDonate"]').click()
        self.page.wait_for_function("!document.getElementById('guildAutoDonate').disabled")
        self.assertTrue(self.page.locator("#guildAutoDonate").is_checked())
        self.assertEqual(self.page.locator("#guildDonateState").inner_text(), "金币 0/50000")
        saved = [body for path, body in self.requests if path == "/api/toggles"]
        self.assertEqual(saved[0]["params"]["guild_auto_donate"],
                         {"gold": True, "courage": False, "reserve": {"gold": 200000, "courage": 10}})
        self.assertTrue(saved[1]["toggles"]["guild_auto_donate"])
        self.assertEqual(saved[1]["account"], "first")
        self.toggle_error = True
        choices.nth(0).click()
        self.page.wait_for_function("(ACCOUNTS.first.logHtml || '').includes('自动捐赠设置保存失败')")
        self.assertTrue(choices.nth(0).locator("input").is_checked())
        self.assertEqual(self.count("/api/guild/overview"), 0)
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild, donating:'gold', revision:3})")
        self.assertEqual(self.page.locator("#guildDonateState").inner_text(), "正在捐赠金币…")
        self.assertTrue(self.page.locator("#guildAutoDonate").is_disabled())
        self.assertEqual(self.page.locator("#guildRefresh").get_attribute("aria-busy"), "false")
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild, donating:null, donationErrors:{gold:'timeout'}, revision:4})")
        self.assertEqual(self.page.locator("#guildDonateState").inner_text(), "捐赠未确认，等待下次登录或手动刷新")
        self.assertEqual(self.page.locator("#guildOverview").count(), 0)
        self.page.evaluate("activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertFalse(self.page.locator("#guildAutoDonate").is_checked())
        self.assertTrue(self.page.locator("#guildAutoDonate").is_disabled())
        self.assertEqual(self.page.locator("#guildDonateState").inner_text(), "未加入佣兵团，暂不执行")

    def test_donation_reserve_controls_save_in_the_same_compact_row(self):
        self.enter_guild()
        self.page.locator("#guildDonateOptions").click()
        rows = self.page.locator("#guildDonateChoices .guild-donate-choice")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.nth(0).locator(".guild-donate-label").inner_text(), "金币")
        self.assertEqual(rows.nth(0).locator(".guild-donate-reserve-label").inner_text(), "保留")
        self.assertEqual(rows.nth(0).locator(".guild-donate-reserve").input_value(), "200000")
        self.assertEqual(rows.nth(1).locator(".guild-donate-reserve").input_value(), "10")
        rows.nth(0).locator(".guild-donate-reserve").fill("50000")
        rows.nth(0).locator(".guild-donate-reserve").press("Tab")
        self.page.wait_for_function("document.querySelector('#guildDonateChoices .guild-donate-reserve').value === '50000'")
        saved = [body for path, body in self.requests if path == "/api/toggles"][-1]
        self.assertEqual(saved["params"]["guild_auto_donate"]["reserve"], {"gold": 50000, "courage": 10})
        self.assertEqual(self.count("/api/guild/overview"), 0)

    def test_explicit_zero_reserves_survive_save_and_account_switch(self):
        self.enter_guild()
        self.page.locator("#guildDonateOptions").click()
        inputs = self.page.locator("#guildDonateChoices .guild-donate-reserve")
        for index in (0, 1):
            inputs.nth(index).fill("0")
            inputs.nth(index).press("Tab")
            self.page.wait_for_function("!document.getElementById('guildAutoDonate').disabled")
        saved = [body for path, body in self.requests if path == "/api/toggles"][-1]
        self.assertEqual(saved["params"]["guild_auto_donate"]["reserve"], {"gold": 0, "courage": 0})
        self.page.evaluate("activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertEqual(inputs.nth(0).input_value(), "200000")
        self.assertEqual(inputs.nth(1).input_value(), "10")
        self.page.evaluate("activeAccount='first'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertEqual(inputs.nth(0).input_value(), "0")
        self.assertEqual(inputs.nth(1).input_value(), "0")

    def test_mobile_desktop_light_dark_layout(self):
        self.enter_guild()
        self.page.locator("#guildDonateOptions").click()
        self.page.locator("#guildAidOptions").click()
        self.page.wait_for_function("!document.querySelector('#guildAidItem .dd-btn').disabled")
        for width in (320, 390, 1280):
            self.page.set_viewport_size({"width": width, "height": 900})
            self.page.evaluate("setDrawer(false)")
            self.page.wait_for_function("!document.body.classList.contains('drawer-open')")
            for theme in ("light", "dark"):
                self.page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                self.page.wait_for_timeout(450)
                self.page.locator("#section-guild").screenshot(path=str(ROOT / "previews" / f"guild-{width}-{theme}.png"))
                for selector in ("#guildRefresh", ".guild-signin-row .switch", "#guildName", "#guildStats", "#guildAvatar",
                                 "#guildDonateOptions", "#guildDonateRow .switch", "#guildDonateChoices",
                                 "#guildAidOptions", "#guildAidRow .switch", ".guild-aid-request .guild-aid-label",
                                 "#guildAidItem", "#guildAidChoices"):
                    rect = self.page.locator(selector).bounding_box()
                    self.assertGreaterEqual(rect["x"], 0)
                    self.assertLessEqual(rect["x"] + rect["width"], width)
                description = self.page.locator(".guild-signin-row .tdesc").bounding_box()
                status = self.page.locator("#guildSigninState").bounding_box()
                self.assertGreaterEqual(status["y"], description["y"] + description["height"])
                self.page.locator('#guildAidItem .dd-btn').click()
                menu = self.page.locator('#guildAidItem .dd-menu')
                menu.evaluate('el => Promise.all(el.getAnimations().map(animation => animation.finished))')
                rect = menu.bounding_box()
                button = self.page.locator('#guildAidItem .dd-btn').bounding_box()
                self.assertAlmostEqual(rect["x"], button["x"], delta=1)
                self.assertGreaterEqual(rect["x"], 0)
                self.assertGreaterEqual(rect["y"], 0)
                self.assertLessEqual(rect["x"] + rect["width"], width)
                self.assertLessEqual(rect["y"] + rect["height"], 900)
                self.assertTrue(menu.evaluate('el => el.scrollWidth <= el.clientWidth'))
                cells = menu.locator('.dd-opt')
                first, second = cells.nth(0).bounding_box(), cells.nth(1).bounding_box()
                self.assertEqual(first['y'], second['y'])
                self.assertGreater(second['x'], first['x'])
                self.page.screenshot(path=str(ROOT / "previews" / f"guild-aid-{width}-{theme}.png"))
                self.page.locator('#guildAidItem .dd-btn').click()

    def test_aid_menu_overlays_next_card_and_remains_interactive(self):
        self.enter_guild()
        # Keep this regression's cross-card overlap geometry as more task rows
        # are added. The menu must still float above the following card.
        self.page.locator('#guildStoreRow').evaluate("el => el.style.display = 'none'")
        self.page.locator('#guildCaptainStoreRow').evaluate("el => el.style.display = 'none'")
        self.page.locator('#guildAidOptions').click()
        self.page.wait_for_function("!document.querySelector('#guildAidItem .dd-btn').disabled")
        self.page.locator('#guildAidItem .dd-btn').click()
        menu = self.page.locator('#guildAidItem .dd-menu')
        menu.evaluate('el => Promise.all(el.getAnimations().map(animation => animation.finished))')
        overlap = menu.evaluate('''el => {
            const rect = el.getBoundingClientRect();
            const next = document.querySelector('.guild-manual-card').getBoundingClientRect();
            const x = rect.x + rect.width / 2, y = next.top + 10;
            return {overlaps: y < rect.bottom, menuOnTop: el.contains(document.elementFromPoint(x, y))};
        }''')
        self.assertTrue(overlap['overlaps'])
        self.assertTrue(overlap['menuOnTop'])
        menu.evaluate('el => el.scrollTop = el.scrollHeight')
        self.assertGreater(menu.evaluate('el => el.scrollTop'), 0)
        last = menu.locator('.dd-opt').last
        selected = last.get_attribute('data-val')
        last.click()
        self.assertFalse(menu.is_visible())
        self.page.wait_for_function("!document.querySelector('#guildAidItem .dd-btn').disabled")
        self.assertEqual(self.page.locator('#guildAidItem .dropdown').get_attribute('data-val'), selected)
        self.page.locator('#guildAidItem .dd-btn').click()
        self.page.locator('.guild-manual-card .card-title').click(position={'x': 5, 'y': 5})
        self.assertFalse(menu.is_visible())

    def test_aid_options_load_local_materials_and_save_without_refreshing_guild(self):
        self.enter_guild()
        self.assertFalse(self.page.locator("#guildAutoAid").is_checked())
        self.page.locator("#guildAidOptions").click()
        self.page.wait_for_function("document.querySelectorAll('#guildAidItem .dd-opt').length === 55")
        for value, checked in (("hunt", True), ("element", True), ("catalyst", False)):
            self.assertEqual(self.page.locator(f'#guildAidChoices input[value="{value}"]').is_checked(), checked)
        self.page.locator("#guildAidItem .dd-btn").click()
        self.assertEqual(self.page.locator('#guildAidItem .dd-menu').inner_text(), '')
        self.assertTrue(self.page.locator('#guildAidItem .dd-opt[data-val="CR54"]').get_attribute('title'))
        self.page.locator('#guildAidItem .dd-opt[data-val="CR54"]').click()
        self.page.wait_for_function("!document.getElementById('guildAutoAid').disabled")
        self.page.locator('#guildAidChoices label').filter(has_text="讨伐材料").click()
        self.page.wait_for_function("!document.getElementById('guildAutoAid').disabled")
        self.page.locator("#guildAidRow .switch-wrap").click()
        self.page.wait_for_function("!document.getElementById('guildAutoAid').disabled")
        body = [body for path, body in self.requests if path == '/api/toggles'][-2]
        self.assertEqual(body["params"]["guild_auto_aid"],
                         {"request": True, "item": "CR54", "hunt": False, "element": True, "catalyst": False})
        body = [body for path, body in self.requests if path == '/api/toggles'][-1]
        self.assertTrue(body["toggles"]["guild_auto_aid"])
        self.assertEqual(self.count('/api/guild/overview'), 0)
        self.assertEqual(self.count('/assets/guild_aid.json'), 1)
        self.assertIn("CR54.png", self.page.locator('#guildAidItem .dd-cur img').get_attribute('src'))
        self.assertEqual(self.page.locator('#guildAidItem .dd-cur').inner_text(),
                         self.page.locator('#guildAidItem .dd-btn').get_attribute('title'))
        self.page.locator('#guildAidChoices label').filter(has_text="催化剂").click()
        self.page.wait_for_function("!document.getElementById('guildAutoAid').disabled")
        body = [body for path, body in self.requests if path == '/api/toggles'][-1]
        self.assertTrue(body['params']['guild_auto_aid']['catalyst'])
        self.page.evaluate("activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertFalse(self.page.locator('#guildAidChoices input[value="catalyst"]').is_checked())
        self.page.evaluate("activeAccount='first'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertTrue(self.page.locator('#guildAidChoices input[value="catalyst"]').is_checked())
        self.page.locator('#guildAidOptions').click()
        self.page.locator('#guildAidOptions').click()
        self.assertEqual(self.count('/assets/guild_aid.json'), 1)

    def test_aid_reselecting_same_material_preserves_selected_name(self):
        self.enter_guild()
        self.page.locator('#guildAidOptions').click()
        self.page.wait_for_function("!document.querySelector('#guildAidItem .dd-btn').disabled")
        for item in ('CR54', 'CR54', 'CR54', 'R008', 'R008', '', '', 'CR54'):
            self.page.locator('#guildAidItem .dd-btn').click()
            self.page.locator(f'#guildAidItem .dd-opt[data-val="{item}"]').click()
            self.page.wait_for_function("!document.querySelector('#guildAidItem .dd-btn').disabled")
            button = self.page.locator('#guildAidItem .dd-btn')
            self.assertEqual(self.page.locator('#guildAidItem .dd-cur').inner_text(), button.get_attribute('title'))
            if item:
                self.assertIn(f'{item}.png', self.page.locator('#guildAidItem .dd-cur img').get_attribute('src'))
            else:
                self.assertEqual(self.page.locator('#guildAidItem .dd-cur').inner_text(), '不指定')
            options = [body for path, body in self.requests if path == '/api/toggles'][-1]['params']['guild_auto_aid']
            self.assertEqual(options['item'], item)
            self.assertEqual(options['request'], bool(item))
        self.assertEqual(self.count('/api/guild/overview'), 0)

    def test_aid_state_and_account_switch_are_isolated(self):
        self.enter_guild()
        self.page.evaluate("""() => {
          ACCOUNTS.first.params.guild_auto_aid={request:true,item:'R008',hunt:true,element:true};
          applyGuildSnapshot('first', {...ACCOUNTS.first.guild, aid:{requested:true,requestKnown:true,reward:200,limit:200},revision:3});
        }""")
        self.assertEqual(self.page.locator('#guildAidState').inner_text(), '今日已求助 · 援助奖励 200/200')
        self.page.evaluate("activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertTrue(self.page.locator('#guildAutoAid').is_disabled())
        self.assertTrue(self.page.locator('#guildAidItem .dd-btn').is_disabled())
        self.assertEqual(self.page.locator('#guildAidState').inner_text(), '未加入佣兵团，暂不执行')
        self.page.evaluate("activeAccount='first'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.page.locator('#guildAidOptions').click()
        self.page.wait_for_function("document.querySelectorAll('#guildAidItem .dd-opt').length === 55")
        self.assertEqual(self.page.locator('#guildAidItem .dropdown').get_attribute('data-val'), 'R008')
        self.page.locator('#guildAidItem .dd-btn').click()
        self.page.locator('#guildAidItem .dd-opt[data-val=""]').click()
        self.page.wait_for_function("!document.getElementById('guildAutoAid').disabled")
        self.assertFalse(self.page.locator('#guildAidItem .dd-btn').is_disabled())
        body = [body for path, body in self.requests if path == '/api/toggles'][-1]
        self.assertEqual(body['params']['guild_auto_aid']['item'], '')
        self.assertFalse(body['params']['guild_auto_aid']['request'])
        self.assertEqual(self.page.locator('#guildAidState').inner_text(), '援助奖励 200/200')

    def test_aid_legacy_disabled_request_stays_unspecified_when_support_scope_changes(self):
        self.enter_guild()
        self.page.evaluate("""() => {
          ACCOUNTS.first.params.guild_auto_aid={request:false,item:'R008',hunt:true,element:true};
          window.dispatchEvent(new CustomEvent('lucima:account-change'));
        }""")
        self.page.locator('#guildAidOptions').click()
        self.page.wait_for_function("!document.querySelector('#guildAidItem .dd-btn').disabled")
        self.assertEqual(self.page.locator('#guildAidItem .dropdown').get_attribute('data-val'), '')
        self.assertEqual(self.page.locator('#guildAidItem .dd-cur').inner_text(), '不指定')
        self.assertEqual(self.page.locator('#guildAidState').inner_text(), '援助奖励 80/200')
        self.assertEqual(self.count('/api/toggles'), 0)
        self.page.locator('#guildAidChoices label').filter(has_text='催化剂').click()
        self.page.wait_for_function("!document.getElementById('guildAutoAid').disabled")
        options = [body for path, body in self.requests if path == '/api/toggles'][-1]['params']['guild_auto_aid']
        self.assertEqual(options, {'request': False, 'item': '', 'hunt': True, 'element': True, 'catalyst': True})
        self.assertEqual(self.count('/api/guild/overview'), 0)

    def test_aid_save_failure_rolls_back_and_running_or_error_status_stays_in_place(self):
        self.enter_guild()
        self.toggle_error = True
        self.page.locator('#guildAidRow .switch-wrap').click()
        self.page.wait_for_function("!document.getElementById('guildAutoAid').disabled")
        self.assertFalse(self.page.locator('#guildAutoAid').is_checked())
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild,aid:{running:'support',reward:80,limit:200},revision:3})")
        self.assertEqual(self.page.locator('#guildAidState').inner_text(), '正在援助团员…')
        self.assertTrue(self.page.locator('#guildRefresh').is_disabled())
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild,aid:{error:'timeout',reward:80,limit:200},revision:4})")
        self.assertEqual(self.page.locator('#guildAidState').inner_text(), '操作未确认，5 分钟后查询确认')
        self.assertEqual(self.count('/api/guild/overview'), 0)

    def test_weekly_details_are_readable_without_switch_or_extra_requests(self):
        self.enter_guild()
        self.assertFalse(self.page.locator('#guildAutoWeekly').is_checked())
        self.assertEqual(self.page.locator('#guildWeeklyState').inner_text(), '个人已领 1/3 · 积分奖励已领 1/6')
        self.page.locator('#guildWeeklyDetails').click()
        self.assertTrue(self.page.locator('#guildWeeklyBody').is_visible())
        self.assertEqual(self.page.locator('.guild-weekly-task').count(), 3)
        self.assertEqual(self.page.locator('.guild-weekly-tier').count(), 6)
        self.assertEqual(self.page.locator('.guild-weekly-task.ready .guild-weekly-count').inner_text(), '12/10')
        self.assertEqual(self.page.locator('.guild-weekly-task.ready [role="progressbar"]').get_attribute('aria-valuenow'), '10')
        self.assertEqual(self.page.locator('.guild-weekly-tier.claimed svg').count(), 1)
        self.assertEqual(self.count('/api/guild/overview'), 0)
        self.assertEqual(self.count('/api/toggles'), 0)
        self.page.wait_for_function("[...document.querySelectorAll('.guild-weekly-reward img')].every(img => img.naturalWidth > 0)")

    def test_weekly_toggle_failure_and_account_isolation(self):
        self.enter_guild()
        self.toggle_error = True
        self.page.locator('#guildWeeklyRow .switch-wrap').click()
        self.page.wait_for_function("!document.getElementById('guildAutoWeekly').disabled")
        self.assertFalse(self.page.locator('#guildAutoWeekly').is_checked())
        self.toggle_error = False
        self.page.locator('#guildWeeklyRow .switch-wrap').click()
        self.page.wait_for_function("!document.getElementById('guildAutoWeekly').disabled")
        body = [body for path, body in self.requests if path == '/api/toggles'][-1]
        self.assertTrue(body['toggles']['guild_auto_weekly'])
        self.page.locator('#guildWeeklyDetails').click()
        self.page.evaluate("activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertTrue(self.page.locator('#guildAutoWeekly').is_disabled())
        self.assertTrue(self.page.locator('#guildWeeklyDetails').is_disabled())
        self.assertEqual(self.page.locator('.guild-weekly-task, .guild-weekly-tier').count(), 0)
        self.assertEqual(self.page.locator('#guildWeeklyState').inner_text(), '未加入佣兵团，暂不执行')
        self.page.evaluate("activeAccount='first'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertTrue(self.page.locator('#guildAutoWeekly').is_checked())
        self.assertEqual(self.page.locator('.guild-weekly-task').count(), 3)
        self.assertEqual(self.count('/api/guild/overview'), 0)

    def test_weekly_unknown_and_unconfirmed_progress_and_running_state(self):
        self.enter_guild()
        self.page.locator('#guildWeeklyDetails').click()
        self.page.evaluate("""applyGuildSnapshot('first', {...ACCOUNTS.first.guild, revision:3,weekly:{
          ...ACCOUNTS.first.guild.weekly, personalKnown:false, personal:[
            {id:'future',name:'<未知任务>',count:null,target:null,state:'unknown'},
            {id:'pending',name:'待确认任务',count:10,target:10,state:'uncertain'}]}})""")
        self.assertIn('<未知任务>', self.page.locator('#guildWeeklyTasks').inner_text())
        self.assertIn('待确认', self.page.locator('#guildWeeklyTasks').inner_text())
        self.assertIn('个人进度等待登录快照更新', self.page.locator('#guildWeeklyTasks').inner_text())
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild,revision:4,weekly:{...ACCOUNTS.first.guild.weekly,running:'tiers'}})")
        self.assertEqual(self.page.locator('#guildWeeklyState').inner_text(), '正在领取全团积分奖励…')
        self.assertTrue(self.page.locator('#guildRefresh').is_disabled())
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild,revision:5,weekly:{...ACCOUNTS.first.guild.weekly,running:null,error:'timeout'}})")
        self.assertEqual(self.page.locator('#guildWeeklyState').inner_text(), '领取未确认，5 分钟后查询确认')

    def test_weekly_layout_on_phone_and_desktop_in_both_themes(self):
        self.enter_guild()
        self.page.locator('#guildWeeklyDetails').click()
        output = ROOT / 'previews'
        output.mkdir(exist_ok=True)
        for width in (320, 390, 1280):
            self.page.set_viewport_size({'width': width, 'height': 950})
            self.page.evaluate('setDrawer(false)')
            self.page.wait_for_function("!document.body.classList.contains('drawer-open')")
            for theme in ('dark', 'light'):
                self.page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", theme)
                self.page.locator('#guildWeeklyRow').scroll_into_view_if_needed()
                self.page.evaluate("Promise.all(document.getAnimations().map(animation => animation.finished))")
                bounds = self.page.locator('#guildWeeklyRow').bounding_box()
                self.assertGreaterEqual(bounds['x'], 0)
                self.assertLessEqual(bounds['x'] + bounds['width'], width)
                self.assertTrue(self.page.locator('#guildWeeklyBody').evaluate('el => el.scrollWidth <= el.clientWidth'))
                if width <= 600:
                    description = self.page.locator('#guildWeeklyRow .tdesc').bounding_box()
                    controls = self.page.locator('#guildWeeklyDetails').bounding_box()
                    self.assertGreaterEqual(description['y'], controls['y'] + controls['height'])
                    self.assertGreater(description['width'], bounds['width'] * .75)
                self.page.locator('#guildWeeklyRow').screenshot(path=str(output / f'guild-weekly-{width}-{theme}.png'))

    def test_score_warning_requires_confirmation_and_can_cancel_or_stop(self):
        self.enter_guild()
        button = self.page.locator('#guildScoreStart')
        self.page.wait_for_function("!document.getElementById('guildScoreStart').disabled")
        self.assertEqual(self.page.locator('.guild-score-warning').inner_text(), '非常规功能')
        self.assertEqual(self.page.locator('#guildScoreState').inner_text(), '全团积分 1200 / 2800')
        button.click()
        self.assertEqual(self.page.locator('#confirmMessage').inner_text(),
                         '该功能非常规功能，请在明确功能含义的情况下执行，建议使用非正式账号')
        self.assertEqual(self.page.locator('.confirm-icon use').get_attribute('href'), '#ui-triangle-alert')
        self.assertEqual(self.count('/api/guild/quests/start'), 0)
        self.page.locator('.confirm-cancel').click()
        self.page.wait_for_function("!document.getElementById('guildScoreStart').disabled")
        self.assertEqual(self.count('/api/guild/quests/start'), 0)
        button.click()
        self.page.locator('#confirmAction').click()
        self.page.wait_for_function("document.getElementById('guildScoreStart').textContent === '停止'")
        self.assertEqual(self.count('/api/guild/quests/start'), 1)
        start = [body for path, body in self.requests if path == '/api/guild/quests/start'][-1]
        self.assertEqual(start['mode'], 'score')
        self.assertTrue(start['requestId'])
        self.assertIn('进行中', self.page.locator('#guildScoreState').inner_text())
        button.click()
        self.page.wait_for_function("document.getElementById('guildScoreStart').textContent === '开始'")
        self.assertIn('已停止', self.page.locator('#guildScoreState').inner_text())
        self.assertEqual(self.count('/api/guild/quests/cancel'), 1)
        self.assertEqual(self.count('/api/guild/overview'), 0)
        self.page.evaluate("() => { openConfirmDialog({title:'删除',message:'测试'}); }")
        self.assertEqual(self.page.locator('.confirm-icon use').get_attribute('href'), '#ui-trash-2')

    def test_score_lost_start_response_is_queried_without_sending_start_again(self):
        self.enter_guild()
        self.score_start_error = True
        self.page.wait_for_function("!document.getElementById('guildScoreStart').disabled")
        self.page.locator('#guildScoreStart').click()
        self.page.locator('#confirmAction').click()
        self.page.wait_for_function("document.getElementById('guildScoreState').textContent.includes('进行中') && document.getElementById('guildScoreStart').textContent === '停止'")
        self.assertEqual(self.count('/api/guild/quests/start'), 1)
        self.assertGreaterEqual(self.count('/api/guild/quests'), 2)
        self.score_jobs['first'].update(state='completed', running=False, completed=64, score=2800)
        self.page.wait_for_function("document.getElementById('guildScoreState').textContent.includes('已完成')")
        self.assertEqual(self.page.locator('#guildScoreState').inner_text(), '全团积分 2800 / 2800 · 已领取 64 次 · 已完成')
        self.assertFalse(self.page.locator('#guildScoreStart').is_disabled())
        self.assertEqual(self.count('/api/guild/quests/start'), 1)

    def test_score_account_switch_or_no_guild_does_not_start_or_leak_task(self):
        self.enter_guild()
        self.page.wait_for_function("!document.getElementById('guildScoreStart').disabled")
        self.page.locator('#guildScoreStart').click()
        self.page.evaluate("activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.page.locator('#confirmAction').click()
        self.assertEqual(self.count('/api/guild/quests/start'), 0)
        self.assertTrue(self.page.locator('#guildScoreStart').is_disabled())
        self.assertEqual(self.page.locator('#guildScoreState').inner_text(), '未加入佣兵团，暂不执行')
        self.score_jobs['first'] = {'id': 'job', 'mode': 'score', 'running': True, 'state': 'running',
                                    'score': 2000, 'target': 2800, 'completed': 32}
        self.page.evaluate("activeAccount='first'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.page.wait_for_function("document.getElementById('guildScoreStart').textContent === '停止'")
        self.assertIn('2000 / 2800', self.page.locator('#guildScoreState').inner_text())
        self.page.evaluate("showSection('info')")
        self.page.wait_for_timeout(1100)
        count = self.count('/api/guild/quests')
        self.page.wait_for_timeout(1100)
        self.assertEqual(self.count('/api/guild/quests'), count)

    def test_score_uses_refreshed_guild_progress_instead_of_stale_finished_job(self):
        self.score_jobs['first'] = {'id': 'old-job', 'mode': 'score', 'running': False, 'state': 'failed',
                                   'score': 80, 'target': 2800, 'completed': 1, 'message': '积分未增长',
                                   'guild': summary(weekly={**weekly_summary(), 'score': 75})}
        self.enter_guild()
        self.page.wait_for_function("!document.getElementById('guildScoreStart').disabled")
        self.assertIn('75 / 2800', self.page.locator('#guildScoreState').inner_text())
        self.assertEqual(self.page.locator('#guildWeeklyScore').text_content(), '75')
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild, revision:3, weekly:{...ACCOUNTS.first.guild.weekly, score:null}})")
        self.assertIn('— / 2800', self.page.locator('#guildScoreState').inner_text())

    def test_score_batch_shows_planned_claims_and_verification_without_estimated_points(self):
        self.score_jobs['first'] = {'id': 'batch', 'mode': 'score', 'running': True, 'state': 'running',
                                   'phase': 'claiming', 'score': 100, 'target': 2800,
                                   'completed': 30, 'planned': 109,
                                   'guild': summary(weekly={**weekly_summary(), 'score': 100})}
        self.enter_guild()
        self.page.wait_for_function("document.getElementById('guildScoreState').textContent.includes('30 / 109')")
        self.assertEqual(self.page.locator('#guildScoreState').inner_text(),
                         '全团积分 100 / 2800 · 已领取 30 / 109 次 · 进行中…')
        self.assertEqual(self.page.locator('#guildWeeklyScore').text_content(), '100')
        self.score_jobs['first'].update(phase='verifying', completed=109)
        self.page.wait_for_function("document.getElementById('guildScoreState').textContent.includes('正在校验')")
        self.assertIn('109 / 109', self.page.locator('#guildScoreState').inner_text())
        self.assertEqual(self.page.locator('#guildWeeklyScore').text_content(), '100')
        for width in (320, 390, 1280):
            self.page.set_viewport_size({'width': width, 'height': 900})
            for theme in ('dark', 'light'):
                self.page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                for selector in ('#guildScoreStart', '#guildScoreState'):
                    rect = self.page.locator(selector).bounding_box()
                    self.assertGreaterEqual(rect['x'], 0)
                    self.assertLessEqual(rect['x'] + rect['width'], width)
                self.assertTrue(self.page.locator('#guildScoreState').evaluate(
                    'element => element.scrollWidth <= element.clientWidth + 1'))
        self.score_jobs['first'].update(state='completed', running=False, score=2800,
                                       guild=summary(weekly={**weekly_summary(), 'score': 2800}))
        self.page.wait_for_function("document.getElementById('guildScoreState').textContent.includes('已完成')")
        self.assertEqual(self.page.locator('#guildWeeklyScore').text_content(), '2,800')

    def test_score_layout_on_phone_and_desktop_in_both_themes(self):
        self.enter_guild()
        self.page.wait_for_function("!document.getElementById('guildScoreStart').disabled")
        output = ROOT / 'previews'
        output.mkdir(exist_ok=True)
        for width in (320, 390, 1280):
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.page.evaluate('setDrawer(false)')
            self.page.wait_for_function("!document.body.classList.contains('drawer-open')")
            for theme in ('dark', 'light'):
                self.page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                self.page.locator('#guildScoreRow').scroll_into_view_if_needed()
                self.page.evaluate("Promise.all(document.getAnimations().map(animation => animation.finished))")
                for selector in ('#guildScoreStart', '.guild-score-warning', '#guildScoreState'):
                    rect = self.page.locator(selector).bounding_box()
                    self.assertGreaterEqual(rect['x'], 0)
                    self.assertLessEqual(rect['x'] + rect['width'], width)
                self.page.locator('.guild-manual-card').screenshot(path=str(output / f'guild-score-{width}-{theme}.png'))
                self.page.locator('#guildScoreStart').click()
                self.page.locator('.confirm-dialog').screenshot(path=str(output / f'guild-score-confirm-{width}-{theme}.png'))
                self.page.locator('.confirm-cancel').click()

    def test_store_split_selection_manual_quantity_and_account_isolation(self):
        self.enter_guild()
        self.page.locator('#guildStoreOptions').click()
        self.page.locator('#guildCaptainStoreOptions').click()
        self.page.locator('#guildManualStoreOptions').click()
        self.assertEqual(self.page.locator('#guildStoreGoods input').count(), 7)
        self.assertEqual(self.page.locator('#guildManualStoreGoods [data-buy]').count(), 5)
        self.assertEqual(self.count('/api/guild/overview'), 0)
        self.assertEqual(self.count('/api/guild/store/buy'), 0)
        self.assertFalse(self.page.locator('#guildAutoStore').is_checked())
        option = self.page.locator('#guildStoreGoods input[value="GuildB015"]')
        option.locator('..').click()
        self.page.wait_for_function("ACCOUNTS.first.params.guild_auto_store?.items.includes('GuildB015')")
        self.assertFalse(self.page.locator('#guildAutoStore').is_checked())
        self.assertEqual(self.count('/api/guild/store/buy'), 0)
        self.page.locator('label[for="guildAutoStore"]').click()
        self.page.wait_for_function('ACCOUNTS.first.toggles.guild_auto_store === true')
        quantity = self.page.locator('[data-quantity="GuildAC01"]')
        quantity.fill('3')
        self.page.locator('[data-buy="GuildAC01"]').click()
        self.page.wait_for_function("!document.querySelector('[data-buy=GuildAC01]').disabled")
        sent = [body for path, body in self.requests if path == '/api/guild/store/buy']
        self.assertEqual(sent, [{'account': 'first', 'id': 'GuildAC01', 'count': 3}])
        self.page.evaluate("activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.assertFalse(self.page.locator('#guildAutoStore').is_checked())
        self.assertTrue(self.page.locator('#guildAutoStore').is_disabled())
        self.assertEqual(self.page.locator('#guildManualStoreGoods [data-buy]').count(), 0)

    def test_store_save_failure_restores_selection_and_quantity_focus_survives_poll(self):
        self.enter_guild()
        self.page.locator('#guildStoreOptions').click()
        self.toggle_error = True
        self.page.locator('#guildStoreGoods input[value="GuildB015"]').locator('..').click()
        self.page.wait_for_function("!document.querySelector('#guildStoreGoods input').disabled")
        self.assertFalse(self.page.locator('#guildStoreGoods input[value="GuildB015"]').is_checked())
        self.toggle_error = False
        self.page.locator('#guildManualStoreOptions').click()
        quantity = self.page.locator('[data-quantity="GuildAC01"]')
        quantity.fill('3')
        self.page.evaluate("applyGuildSnapshot('first', {...ACCOUNTS.first.guild, revision:20})")
        self.assertTrue(quantity.evaluate('el => el === document.activeElement'))
        self.assertEqual(quantity.input_value(), '3')
        self.assertEqual(self.count('/api/guild/store/buy'), 0)

    def test_captain_store_requires_leader_or_deputy(self):
        self.enter_guild()
        self.page.evaluate("""() => {
            const guild = {...ACCOUNTS.first.guild, captainStore: {
                ...ACCOUNTS.first.guild.captainStore,
                access: {allowed:false, authority:3, reason:'团长商店仅限团长和副团长购买'}
            }};
            applyGuildSnapshot('first', guild);
        }""")
        self.assertTrue(self.page.locator('#guildAutoCaptainStore').is_disabled())
        self.assertTrue(self.page.locator('#guildCaptainStoreOptions').is_disabled())
        self.assertTrue(self.page.locator('#guildCaptainStoreGoods input').first.is_disabled())
        self.assertIn('仅限团长和副团长', self.page.locator('#guildCaptainStoreState').text_content())

    def test_captain_store_shares_product_cards_and_updates_balances_without_losing_choices(self):
        self.enter_guild()
        self.page.locator('#guildCaptainStoreOptions').click()
        self.assertEqual(self.page.locator('#guildCaptainStoreGoods .guild-store-price').count(), 5)
        box = self.page.locator('#guildCaptainStoreGoods input[value="GuildBox"]').locator('..')
        self.assertEqual(box.locator('.guild-store-price').inner_text(), '300')
        self.assertEqual(box.locator('.guild-store-price img').get_attribute('src'), '/assets/items/81.png')
        energy = self.page.locator('#guildCaptainStoreGoods input[value="GuildEnergy30"]').locator('..')
        self.assertEqual(energy.locator('.guild-store-price').inner_text(), '1000000')
        self.assertEqual(energy.locator('.guild-store-price img').get_attribute('src'), '/assets/items/80.png')
        box.click()
        self.page.wait_for_function("ACCOUNTS.first.params.guild_auto_captain_store?.items.includes('GuildBox')")
        self.page.evaluate("""applyGuildSnapshot('first', {...ACCOUNTS.first.guild, revision:20,
            captainStore:{...ACCOUNTS.first.guild.captainStore, currencies:{'80':204000,'81':0}}})""")
        self.assertEqual(self.page.locator('#guildCaptainStoreBalances b').all_text_contents(), ['204000', '0'])
        self.assertTrue(self.page.locator('#guildCaptainStoreGoods input[value="GuildBox"]').is_checked())
        self.assertEqual(self.count('/api/guild/store/buy'), 0)

    def test_store_layout_mobile_and_desktop(self):
        self.enter_guild()
        self.page.locator('#guildStoreOptions').click()
        self.page.locator('#guildCaptainStoreOptions').click()
        self.page.locator('#guildManualStoreOptions').click()
        output = ROOT / 'previews'
        for width in (320, 390, 1280):
            self.page.set_viewport_size({'width': width, 'height': 1000})
            self.page.evaluate('setDrawer(false)')
            for theme in ('dark', 'light'):
                self.page.evaluate('theme => document.documentElement.dataset.theme = theme', theme)
                for selector in ('#guildStoreRow', '#guildCaptainStoreRow', '#guildManualStoreRow'):
                    self.page.locator(selector).scroll_into_view_if_needed()
                    self.page.evaluate('Promise.allSettled(document.getAnimations().map(animation => animation.finished))')
                    self.assertTrue(self.page.locator(selector).evaluate(
                        'el => [...el.querySelectorAll(".guild-store-good, .mini, input[type=number]")].every(c => { const r = c.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })'))
                    self.assertTrue(self.page.locator(selector).evaluate('el => el.scrollWidth <= el.clientWidth + 1'))
                    title = self.page.locator(selector + ' .tname').bounding_box()
                    button = self.page.locator(selector + ' .auto-head > .mini').bounding_box()
                    self.assertLess(title['y'], button['y'] + button['height'])
                    self.assertLess(button['y'], title['y'] + title['height'])
                    self.assertLess(title['height'], 30)
                    self.page.locator(selector).screenshot(path=str(output / f'{selector[1:]}-{width}-{theme}.png'))


if __name__ == "__main__":
    unittest.main()
