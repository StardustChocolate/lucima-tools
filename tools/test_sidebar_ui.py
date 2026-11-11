"""Check navigation and account access in short desktop and mobile viewports."""
import json
import mimetypes
from pathlib import Path
import unittest
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://sidebar.invalid'


class SidebarUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.errors, self.unexpected = [], []
        self.context = self.browser.new_context(viewport={'width': 1280, 'height': 800}, has_touch=True)
        self.addCleanup(self.context.close)
        self.context.route('**/*', self.serve)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(ORIGIN)
        self.page.wait_for_function("updateStartupReady && $('toolboxHome').dataset.bound")
        self.page.evaluate('''() => {
            for (let i = 0; i < 12; i++) rememberAccount({
                account:`user-${i}@example.invalid`, status:{name:`测试账号 ${i}`}
            });
            activeAccount = 'user-0@example.invalid';
            enterApp(); renderAccountSwitcher();
        }''')

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])

    def serve(self, route):
        url = urlsplit(route.request.url)
        path = unquote(url.path)
        if url.netloc != urlsplit(ORIGIN).netloc:
            self.unexpected.append(route.request.url)
            route.abort()
            return
        if path.startswith('/api/'):
            responses = {
                '/api/accounts': {'accounts': []}, '/api/preferences': {'settings': {}},
                '/api/config': {'platform': 'android', 'version': 'test'},
                '/api/update/status': {'testServer': True, 'available': False},
                '/api/update/check': {'testServer': True, 'available': False},
                '/api/resources/status': {'phase': 'ready'},
            }
            if path not in responses:
                self.unexpected.append(path)
            route.fulfill(content_type='application/json', body=json.dumps(responses.get(path, {})))
            return
        file = ROOT / 'frontend/index.html' if path == '/' else (
            ROOT / 'frontend' / path.removeprefix('/static/') if path.startswith('/static/')
            else ROOT / path.lstrip('/'))
        if not file.resolve().is_relative_to(ROOT) or not file.is_file():
            self.unexpected.append(path)
            route.abort()
            return
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def viewport(self, width, height):
        self.page.set_viewport_size({'width': width, 'height': height})
        self.page.evaluate('setDrawer(true)')
        self.settle('#sidebar')

    def settle(self, selector):
        self.page.locator(selector).evaluate('''async node => {
            await Promise.all(node.getAnimations().map(animation => animation.finished));
        }''')

    def assert_inside_viewport(self, selector):
        rect = self.page.locator(selector).bounding_box()
        self.assertIsNotNone(rect)
        self.assertGreaterEqual(rect['x'], -1)
        self.assertGreaterEqual(rect['y'], -1)
        self.assertLessEqual(rect['x'] + rect['width'], self.page.viewport_size['width'] + 1)
        self.assertLessEqual(rect['y'] + rect['height'], self.page.viewport_size['height'] + 1)

    def test_normal_desktop_has_all_navigation_and_footer_visible(self):
        for selector in ('.side-brand', '#sideNav .nav-item:first-child',
                         '#sideNav .nav-item:last-child', '#accCurrentBtn', '#logoutBtn'):
            self.assert_inside_viewport(selector)
        self.assertFalse(self.page.locator('#sideNav').evaluate('node => node.scrollHeight > node.clientHeight'))
        expect(self.page.locator('#menuBtn')).to_be_hidden()

    def test_short_landscape_scrolls_navigation_and_keeps_account_in_place(self):
        self.viewport(1024, 390)
        self.assertTrue(self.page.locator('#sideNav').evaluate('node => node.scrollHeight > node.clientHeight'))
        self.assertFalse(self.page.locator('#sidebar').evaluate('node => node.scrollHeight > node.clientHeight'))
        before = self.page.locator('#accCurrentBtn').bounding_box()
        self.page.locator('#sideNav').hover()
        self.page.mouse.wheel(0, 700)
        self.page.wait_for_function("$('sideNav').scrollTop > 100")
        self.assertEqual(self.page.locator('#accCurrentBtn').bounding_box(), before)
        self.assert_inside_viewport('#sideNav .nav-item:last-child')
        self.assert_inside_viewport('#logoutBtn')
        heights = self.page.locator('#sideNav .nav-item').evaluate_all('nodes => nodes.map(node => node.offsetHeight)')
        self.assertTrue(all(height >= 44 for height in heights))

    def test_short_mobile_drawer_can_reach_last_page_and_close(self):
        self.viewport(740, 360)
        self.page.evaluate('setDrawer(false)')
        self.settle('#sidebar')
        self.page.locator('#menuBtn').tap()
        self.settle('#sidebar')
        self.page.locator('#sideNav [data-section="about"]').tap()
        expect(self.page.locator('#section-about')).to_be_visible()
        expect(self.page.locator('#sidebar')).to_have_class('sidebar collapsed')
        expect(self.page.locator('#sidebarBackdrop')).to_be_hidden()
        self.page.locator('#menuBtn').tap()
        self.settle('#sidebar')
        self.assert_inside_viewport('#accCurrentBtn')
        self.assert_inside_viewport('#logoutBtn')

    def test_extremely_short_viewport_scrolls_entire_sidebar_to_footer(self):
        self.viewport(640, 240)
        self.assertTrue(self.page.locator('#sidebar').evaluate('node => node.scrollHeight > node.clientHeight'))
        self.page.locator('#logoutBtn').scroll_into_view_if_needed()
        self.assertGreater(self.page.locator('#sidebar').evaluate('node => node.scrollTop'), 100)
        self.assert_inside_viewport('#accCurrentBtn')
        self.assert_inside_viewport('#logoutBtn')
        self.page.locator('#accCurrentBtn').tap()
        self.settle('#accPop')
        self.assert_inside_viewport('#accPop')
        self.page.locator('#accAddBtn').scroll_into_view_if_needed()
        self.assert_inside_viewport('#accAddBtn')

    def test_many_accounts_fit_and_scroll_without_moving_sidebar(self):
        self.viewport(740, 360)
        self.page.locator('#accCurrentBtn').tap()
        self.settle('#accPop')
        self.assert_inside_viewport('#accPop')
        before = self.page.locator('#accCurrentBtn').bounding_box()
        self.assertTrue(self.page.locator('#accPop').evaluate('node => node.scrollHeight > node.clientHeight'))
        self.page.locator('#accAddBtn').scroll_into_view_if_needed()
        self.assert_inside_viewport('#accAddBtn')
        self.assertEqual(self.page.locator('#accCurrentBtn').bounding_box(), before)
        self.page.locator('#accAddBtn').tap()
        expect(self.page.locator('#loginView')).to_be_visible()

    def test_open_account_list_repositions_after_viewport_change(self):
        self.page.locator('#accCurrentBtn').tap()
        self.settle('#accPop')
        self.page.set_viewport_size({'width': 1024, 'height': 390})
        self.page.wait_for_function("$('accPop').getBoundingClientRect().top >= 0")
        self.assert_inside_viewport('#accPop')
        self.assert_inside_viewport('#accCurrentBtn')
        self.page.locator('#sidebar').evaluate('node => node.scrollTop = node.scrollHeight')
        self.page.set_viewport_size({'width': 1024, 'height': 800})
        self.page.wait_for_function("$('sidebar').clientHeight === 800")
        self.assert_inside_viewport('#accPop')
        self.assert_inside_viewport('#logoutBtn')


if __name__ == '__main__':
    unittest.main()
