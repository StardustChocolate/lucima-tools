"""About checks run on entry; the status icon opens available updates."""
import json
import mimetypes
from pathlib import Path
import unittest
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://about-update.invalid'


class AboutUpdateUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.state = {
            'checked': True, 'checking': False, 'available': False, 'forced': False,
            'currentVersion': '1.3.3', 'latestVersion': '1.3.3', 'lastCheckedAt': 1,
            'lastError': '', 'artifactAvailable': True, 'installSupported': True,
        }
        self.checks = 0
        self.hold_checks = False
        self.held_checks = []
        self.hold_status = False
        self.held_status = []
        self.errors = []
        self.unexpected = []
        self.context = self.browser.new_context(viewport={'width': 393, 'height': 852}, has_touch=True)
        self.addCleanup(self.context.close)
        self.context.route('**/*', self.serve)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(ORIGIN)
        self.page.wait_for_function('updateStartupReady && !updateStartupCheckPending')
        self.page.evaluate('enterApp()')

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])

    def fulfill(self, route, state=None):
        route.fulfill(content_type='application/json', body=json.dumps(self.state if state is None else state))

    def serve(self, route):
        url = urlsplit(route.request.url)
        path = unquote(url.path)
        if url.netloc != urlsplit(ORIGIN).netloc:
            self.unexpected.append(route.request.url)
            route.abort()
            return
        if path == '/api/update/check':
            self.checks += 1
            if self.hold_checks:
                self.held_checks.append(route)
            else:
                self.fulfill(route)
            return
        if path == '/api/update/status':
            if self.hold_status:
                self.held_status.append(route)
            else:
                self.fulfill(route)
            return
        if path.startswith('/api/'):
            responses = {
                '/api/accounts': {'accounts': []}, '/api/preferences': {'settings': {}},
                '/api/config': {'platform': 'android', 'version': '1.3.3'},
                '/api/resources/status': {'phase': 'ready'},
            }
            if path not in responses:
                self.unexpected.append(path)
            self.fulfill(route, responses.get(path, {}))
            return
        file = ROOT / 'frontend/index.html' if path == '/' else (
            ROOT / 'frontend' / path.removeprefix('/static/') if path.startswith('/static/')
            else ROOT / path.lstrip('/'))
        if not file.resolve().is_relative_to(ROOT) or not file.is_file():
            self.unexpected.append(path)
            route.abort()
            return
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def open_about(self):
        self.page.evaluate("showSection('about')")
        self.page.wait_for_function('!updateAboutChecking')
        self.wait_page_entry()

    def wait_page_entry(self):
        self.page.locator('#section-about').evaluate('''async section => {
            await Promise.all(section.getAnimations().map(animation => animation.finished));
        }''')

    def release_checks(self):
        self.page.wait_for_function("document.getElementById('aboutUpdateBtn').disabled")
        self.assertTrue(self.held_checks)
        self.hold_checks = False
        for route in self.held_checks:
            self.fulfill(route)
        self.held_checks.clear()
        self.page.wait_for_function('!updateAboutChecking')

    def test_entry_always_checks_silently_and_manual_click_shows_toast(self):
        count = self.checks
        self.open_about()
        self.assertEqual(self.checks, count + 1)
        expect(self.page.locator('#aboutUpdateBtn')).to_have_attribute('data-state', 'latest')
        expect(self.page.locator('#messageToastRegion')).to_be_empty()
        self.page.evaluate("showSection('info'); showSection('about')")
        self.page.wait_for_function('!updateAboutChecking')
        self.assertEqual(self.checks, count + 2)
        self.page.locator('#aboutUpdateBtn').tap()
        expect(self.page.locator('#messageToastRegion')).to_contain_text('已是最新版本 1.3.3')
        self.assertEqual(self.checks, count + 3)

    def test_available_icon_has_no_frame_and_opens_dialog_without_checking(self):
        self.state.update(available=True, latestVersion='1.3.4', notes=['示例更新'])
        self.open_about()
        expect(self.page.locator('#updateModal')).to_be_hidden()
        expect(self.page.locator('#messageToastRegion')).to_be_empty()
        expect(self.page.locator('#aboutUpdateBtn')).to_have_attribute('data-state', 'update')
        appearance = self.page.locator('#aboutUpdateBtn').evaluate('''button => [button, button.firstElementChild].map(node => {
            const css = getComputedStyle(node);
            return {background:css.backgroundColor, border:css.borderTopWidth, shadow:css.boxShadow};
        })''')
        for node in appearance:
            self.assertEqual(node, {'background': 'rgba(0, 0, 0, 0)', 'border': '0px', 'shadow': 'none'})
        count = self.checks
        self.page.locator('#aboutUpdateBtn').tap()
        expect(self.page.locator('#updateModal')).to_be_visible()
        expect(self.page.locator('#updateTitle')).to_have_text('LucimaTools 1.3.4')
        self.assertEqual(self.checks, count)

    def test_checking_is_stable_and_reentry_coalesces_requests(self):
        self.open_about()
        before = self.page.locator('.about-version-row').bounding_box()
        self.hold_checks = True
        count = self.checks
        self.page.evaluate("showSection('info'); showSection('about')")
        expect(self.page.locator('#aboutUpdateBtn')).to_have_attribute('data-state', 'checking')
        expect(self.page.locator('#aboutUpdateBtn')).to_be_disabled()
        self.page.evaluate("renderUpdateAvailability({checked:true}); showSection('about')")
        expect(self.page.locator('#aboutUpdateBtn')).to_have_attribute('data-state', 'checking')
        self.assertEqual(self.checks, count + 1)
        self.wait_page_entry()
        self.assertEqual(self.page.locator('.about-version-row').bounding_box(), before)
        self.release_checks()
        expect(self.page.locator('#aboutUpdateBtn')).to_be_enabled()
        self.assertEqual(self.page.locator('.about-version-row').bounding_box(), before)

    def test_manual_discovery_shows_toast_then_icon_opens_the_update(self):
        self.open_about()
        self.state.update(available=True, latestVersion='1.3.4')
        self.page.locator('#aboutUpdateBtn').tap()
        expect(self.page.locator('#messageToastRegion')).to_contain_text('发现新版本 1.3.4')
        expect(self.page.locator('#updateModal')).to_be_hidden()
        count = self.checks
        self.page.locator('#aboutUpdateBtn').tap()
        expect(self.page.locator('#updateModal')).to_be_visible()
        self.assertEqual(self.checks, count)

    def test_slow_background_response_cannot_replace_a_newer_about_check(self):
        self.hold_status = True
        self.page.evaluate('void refreshUpdateStatus(false)')
        self.page.wait_for_timeout(50)
        self.assertTrue(self.held_status)
        self.state.update(available=True, latestVersion='1.3.4')
        self.open_about()
        self.hold_status = False
        for route in self.held_status:
            self.fulfill(route, {'checked': True, 'available': False, 'currentVersion': '1.3.3'})
        self.held_status.clear()
        self.page.wait_for_function('!updateStatusPending')
        expect(self.page.locator('#aboutUpdateBtn')).to_have_attribute('data-state', 'update')

    def test_error_is_silent_on_entry_and_manual_retry_reports_result(self):
        self.state['lastError'] = '网络连接失败'
        self.open_about()
        expect(self.page.locator('#aboutUpdateBtn')).to_have_attribute('data-state', 'error')
        expect(self.page.locator('#messageToastRegion')).to_be_empty()
        self.page.locator('#aboutUpdateBtn').tap()
        expect(self.page.locator('#messageToastRegion')).to_contain_text('网络连接失败')
        self.state['lastError'] = ''
        self.page.locator('#aboutUpdateBtn').tap()
        expect(self.page.locator('#aboutUpdateBtn')).to_have_attribute('data-state', 'latest')

    def test_forced_update_still_opens_the_existing_blocking_dialog(self):
        self.state.update(available=True, forced=True, latestVersion='1.3.4')
        self.open_about()
        expect(self.page.locator('#updateModal')).to_be_visible()
        expect(self.page.locator('#updateIgnoreBtn')).to_be_hidden()

    def test_version_center_and_spinner_center_are_fixed_across_themes_and_widths(self):
        self.open_about()
        for width, theme in [(320, 'light'), (393, 'dark'), (1365, 'dark')]:
            with self.subTest(width=width, theme=theme):
                self.page.set_viewport_size({'width': width, 'height': 852})
                self.page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                self.hold_checks = True
                self.page.locator('#aboutUpdateBtn').click()
                measurement = self.page.locator('.about-version-row').evaluate('''async row => {
                    const version = document.getElementById('aboutVer');
                    const face = row.querySelector('.about-update-face');
                    const svg = row.querySelector('.about-update-rotor svg');
                    const frames = [];
                    for (let i=0;i<8;i++) {
                        await new Promise(requestAnimationFrame);
                        const r=row.getBoundingClientRect(), v=version.getBoundingClientRect(), f=face.getBoundingClientRect();
                        const p=svg.createSVGPoint();p.x=12;p.y=12;
                        const c=p.matrixTransform(svg.getScreenCTM());
                        frames.push({versionError:v.x+v.width/2-r.x-r.width/2, cx:c.x-f.x-f.width/2, cy:c.y-f.y-f.height/2, y:r.y});
                    }
                    return frames;
                }''')
                for frame in measurement:
                    self.assertLess(abs(frame['versionError']), .05)
                    self.assertLess(abs(frame['cx']), .05)
                    self.assertLess(abs(frame['cy']), .05)
                self.assertEqual(len({f['y'] for f in measurement}), 1)
                self.release_checks()


if __name__ == '__main__':
    unittest.main()
