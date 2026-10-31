"""Account deletion uses the app dialog even when native JS dialogs are unavailable."""
import json
import mimetypes
from pathlib import Path
import unittest
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://account-delete.invalid'


class AccountDeleteUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.saved = [{'account': email, 'name': name, 'hasToken': True}
                      for email, name in [('first@example.com', '甲'), ('second@example.com', '乙')]]
        self.deletions, self.errors, self.unexpected = [], [], []
        self.delete_error = False
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844}, has_touch=True)
        self.addCleanup(self.context.close)
        self.context.add_init_script("window.confirm = () => { throw new Error('Native confirm unavailable'); };")
        self.context.route('**/*', self.serve)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(ORIGIN)
        self.page.wait_for_function("updateStartupReady && SAVED_ACCTS.length === 2 && $('toolboxHome').dataset.bound")

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
        if path == '/api/accounts/delete':
            self.deletions.append(route.request.post_data_json)
            if self.delete_error:
                route.fulfill(status=500, content_type='application/json', body=json.dumps({'detail': '测试删除失败'}))
                return
            email = self.deletions[-1]['account']
            self.saved = [a for a in self.saved if a['account'] != email]
            route.fulfill(content_type='application/json', body='{"ok":true}')
            return
        if path.startswith('/api/'):
            responses = {
                '/api/accounts': {'accounts': self.saved}, '/api/preferences': {'settings': {}},
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

    def open_saved_delete(self):
        if not self.page.locator('#savedAccts').is_visible():
            self.page.locator('#acctCaret').click()
        self.page.locator('.sa-del[data-del="first@example.com"]').tap()
        expect(self.page.locator('#confirmModal')).to_be_visible()
        expect(self.page.locator('#confirmMessage')).to_contain_text('first@example.com')
        self.assertEqual(self.deletions, [])

    def show_online_account(self):
        self.page.evaluate("""() => {
            rememberAccount({account:'first@example.com', status:{name:'甲'}});
            activeAccount='first@example.com'; enterApp(); renderAccountSwitcher(); setDrawer(true);
        }""")
        self.page.locator('#accCurrentBtn').click()

    def test_login_cancel_button_backdrop_and_escape_never_delete(self):
        for cancel in ('button', 'backdrop', 'escape'):
            self.open_saved_delete()
            if cancel == 'escape':
                self.page.keyboard.press('Escape')
            else:
                selector = '.confirm-actions [data-confirm-cancel]' if cancel == 'button' else '.confirm-backdrop'
                self.page.locator(selector).click(position={'x': 5, 'y': 5})
            expect(self.page.locator('#confirmModal')).to_be_hidden()
            self.assertEqual(self.deletions, [])
            self.assertEqual(self.page.evaluate('SAVED_ACCTS.length'), 2)

    def test_login_confirm_deletes_only_selected_account(self):
        self.open_saved_delete()
        self.page.locator('#confirmAction').tap()
        self.page.wait_for_function('SAVED_ACCTS.length === 1')
        self.assertEqual(self.deletions, [{'account': 'first@example.com'}])
        self.assertEqual(self.page.evaluate('SAVED_ACCTS[0].account'), 'second@example.com')
        self.assertEqual(self.page.locator('.sa-del[data-del="first@example.com"]').count(), 0)
        expect(self.page.locator('#loginView')).to_be_visible()

    def test_login_failed_delete_keeps_account_and_shows_toast(self):
        self.delete_error = True
        self.open_saved_delete()
        self.page.locator('#confirmAction').tap()
        expect(self.page.locator('#messageToastRegion')).to_contain_text('删除账号失败：测试删除失败')
        self.assertEqual(self.page.evaluate('SAVED_ACCTS.length'), 2)
        self.assertEqual(self.page.locator('.sa-del[data-del="first@example.com"]').count(), 1)

    def test_online_cancel_then_confirm_uses_same_dialog(self):
        self.show_online_account()
        self.page.locator('.acc-x[data-del="first@example.com"]').tap()
        expect(self.page.locator('#confirmModal')).to_be_visible()
        expect(self.page.locator('#confirmMessage')).to_contain_text('将下线 first@example.com')
        self.assertEqual(self.deletions, [])
        self.page.locator('.confirm-actions [data-confirm-cancel]').tap()
        self.assertTrue(self.page.evaluate("!!ACCOUNTS['first@example.com']"))
        self.page.evaluate('setDrawer(true)')
        self.page.locator('#accCurrentBtn').click()
        self.page.locator('.acc-x[data-del="first@example.com"]').tap()
        self.page.locator('#confirmAction').tap()
        self.page.wait_for_function("!ACCOUNTS['first@example.com']")
        expect(self.page.locator('#loginView')).to_be_visible()
        self.assertEqual(self.deletions, [{'account': 'first@example.com'}])
        self.assertEqual(self.page.evaluate('SAVED_ACCTS[0].account'), 'second@example.com')

    def test_online_failed_delete_preserves_connected_account(self):
        self.delete_error = True
        self.show_online_account()
        self.page.locator('.acc-x[data-del="first@example.com"]').tap()
        self.page.locator('#confirmAction').tap()
        expect(self.page.locator('#messageToastRegion')).to_contain_text('删除账号失败：测试删除失败')
        self.assertTrue(self.page.evaluate("!!ACCOUNTS['first@example.com']"))
        self.assertEqual(self.page.evaluate('activeAccount'), 'first@example.com')
        self.assertEqual(self.page.evaluate('SAVED_ACCTS.length'), 2)


if __name__ == '__main__':
    unittest.main()
