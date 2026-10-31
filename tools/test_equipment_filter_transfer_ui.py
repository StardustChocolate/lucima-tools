"""Offline browser coverage for equipment scheme file and clipboard transfers."""
import copy
import json
import mimetypes
from pathlib import Path
import unittest
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://filter-transfer.invalid"
SCHEMES = [
    {"id": "first-plan", "name": "秘密商店", "slots": {}},
    {"id": "second-plan", "name": "星源商店", "slots": {}},
]
IMPORTED = {
    "format": "ark-equipment-filter-scheme", "version": 2,
    "scheme": {"id": "external-plan", "name": "分享方案", "slots": {
        "weapon": [{"sets": ["Speed"], "minScore": 30, "scoreEnabled": True}],
    }},
}
CONTENTS = json.dumps(IMPORTED, ensure_ascii=False)


class EquipmentFilterTransferUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors, self.unexpected, self.exports = [], [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route("**/*", self.serve)
        self.page.goto(ORIGIN)
        self.page.wait_for_function("typeof enterApp === 'function' && updateStartupReady")
        self.page.evaluate("""async schemes => {
            rememberAccount({account:'first', status:{name:'first'}});
            activeAccount='first'; enterApp(); await loadTasks();
            showSection('toolbox'); showToolboxPage('equipment-filter');
            EQUIP_FILTER_SCHEMES_DATA=schemes.map(s => normalizeEquipmentFilterScheme(s));
            activeFilterSchemeId=schemes[0].id;
            renderEquipmentFilterWorkspace();
            window.copiedPlans=[]; window.clipboardContents='';
            Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{
                writeText:async text => {window.copiedPlans.push(text)},
                readText:async () => window.clipboardContents,
            }});
        }""", SCHEMES)

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
        if path.startswith("/api/"):
            responses = {
                "/api/accounts": {"accounts": []}, "/api/preferences": {"settings": {}},
                "/api/config": {"platform": "windows", "version": "test"},
                "/api/tasks": {"toggle": [], "manual": [], "buyTargets": []},
                "/api/update/status": {"testServer": True, "available": False},
                "/api/update/check": {"testServer": True, "available": False},
                "/api/resources/status": {"phase": "ready"},
                "/api/logs/auto": {"entries": [], "logSeq": 0},
                "/api/status": {"status": {}, "nextClaims": {}},
                "/api/equipment-filter/export": {"ok": True, "path": "Downloads/方案.json"},
            }
            if path == "/api/equipment-filter/export":
                self.exports.append(route.request.post_data_json)
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

    def menu(self, action, scheme="first-plan"):
        anchor = self.page.locator(f'[data-scheme-id="{scheme}"] [data-scheme-action="{action}"]')
        anchor.click()
        expect(self.page.locator("#schemeTransferMenu")).to_be_visible()
        return anchor

    def choose(self, method):
        self.page.locator(f'#schemeTransferMenu [data-scheme-transfer="{method}"]').click()

    def schemes(self):
        return self.page.evaluate("EQUIP_FILTER_SCHEMES_DATA")

    def open_clipboard(self, contents=CONTENTS):
        self.page.evaluate("text => window.clipboardContents=text", contents)
        self.menu("import")
        self.choose("clipboard")
        expect(self.page.locator("#schemeClipboardModal")).to_be_visible()
        expect(self.page.locator("#schemeClipboardInput")).to_have_value(contents)

    def test_export_menu_defers_action_and_round_trips_same_format(self):
        self.menu("export")
        self.assertEqual(self.page.evaluate("copiedPlans"), [])
        self.assertEqual(self.exports, [])
        self.choose("clipboard")
        self.page.wait_for_function("copiedPlans.length === 1")
        copied = json.loads(self.page.evaluate("copiedPlans[0]"))
        self.assertEqual(copied, {"format": "ark-equipment-filter-scheme", "version": 2, "scheme": SCHEMES[0]})
        self.assertEqual(self.exports, [])
        self.menu("export")
        self.choose("file")
        self.page.wait_for_function("document.querySelector('[data-scheme-action=export]').classList.contains('action-success')")
        self.assertEqual(self.exports, [copied])

    def test_clipboard_cancel_invalid_and_confirm_preserve_target_identity(self):
        before = self.schemes()
        self.open_clipboard()
        self.assertEqual(self.schemes(), before)
        self.page.locator("#schemeClipboardModal .scheme-import-cancel").click()
        self.assertEqual(self.schemes(), before)
        self.open_clipboard("not json")
        self.page.locator("#schemeClipboardConfirm").click()
        expect(self.page.locator("#schemeClipboardError")).to_contain_text("无法解析 JSON")
        self.assertEqual(self.schemes(), before)
        self.page.locator("#schemeClipboardInput").fill(CONTENTS)
        self.page.locator("#schemeClipboardConfirm").click()
        expect(self.page.locator("#schemeClipboardModal")).to_be_hidden()
        imported, untouched = self.schemes()
        self.assertEqual(imported["id"], "first-plan")
        self.assertEqual(imported["name"], "分享方案")
        self.assertEqual(imported["slots"]["weapon"][0]["minScore"], 30)
        self.assertEqual(untouched, before[1])

    def test_clipboard_rejection_allows_manual_paste_and_copy_fallback(self):
        self.page.evaluate("""() => {
            navigator.clipboard.readText=async () => {throw new Error('denied')};
            navigator.clipboard.writeText=async () => {throw new Error('denied')};
            document.execCommand=command => {
                if (command !== 'copy') return false;
                window.copiedPlans.push(document.activeElement.value); return true;
            };
        }""")
        self.menu("import")
        self.choose("clipboard")
        self.page.locator("#schemeClipboardPaste").click()
        expect(self.page.locator("#schemeClipboardError")).to_contain_text("手动粘贴")
        self.page.locator("#schemeClipboardInput").fill(CONTENTS)
        self.page.locator("#schemeClipboardConfirm").click()
        expect(self.page.locator("#schemeClipboardModal")).to_be_hidden()
        self.menu("export")
        self.choose("clipboard")
        self.page.wait_for_function("copiedPlans.length === 1")
        self.assertEqual(json.loads(self.page.evaluate("copiedPlans[0]"))["scheme"]["name"], "分享方案")

    def test_late_clipboard_reads_cannot_overwrite_edits_or_a_new_dialog(self):
        self.page.evaluate("""() => {
            window.clipboardResolvers=[];
            navigator.clipboard.readText=() => new Promise(resolve => clipboardResolvers.push(resolve));
        }""")
        self.menu("import")
        self.choose("clipboard")
        self.page.locator("#schemeClipboardInput").fill("edited")
        self.page.locator("#schemeClipboardInput").fill("")
        self.page.evaluate("text => clipboardResolvers[0](text)", CONTENTS)
        expect(self.page.locator("#schemeClipboardPaste")).to_be_enabled()
        expect(self.page.locator("#schemeClipboardInput")).to_have_value("")
        self.page.locator("#schemeClipboardPaste").click()
        self.page.keyboard.press("Escape")
        self.menu("import")
        self.choose("clipboard")
        self.page.evaluate("text => clipboardResolvers[1](text)", CONTENTS)
        expect(self.page.locator("#schemeClipboardInput")).to_have_value("")
        self.page.evaluate("text => clipboardResolvers[2](text)", CONTENTS)
        expect(self.page.locator("#schemeClipboardInput")).to_have_value(CONTENTS)
        self.assertEqual(self.schemes(), SCHEMES)

    def test_bad_version_and_oversized_text_do_not_change_schemes(self):
        bad_version = copy.deepcopy(IMPORTED)
        bad_version["version"] = 999
        self.open_clipboard(json.dumps(bad_version))
        self.page.locator("#schemeClipboardConfirm").click()
        expect(self.page.locator("#schemeClipboardError")).to_contain_text("请先更新工具")
        self.page.locator("#schemeClipboardInput").fill("羁" * 350000)
        self.page.locator("#schemeClipboardConfirm").click()
        expect(self.page.locator("#schemeClipboardError")).to_contain_text("超过 1 MB")
        self.assertEqual(self.schemes(), SCHEMES)

    def test_browser_file_import_still_uses_file_picker(self):
        self.menu("import")
        with self.page.expect_file_chooser() as chooser:
            self.choose("file")
        chooser.value.set_files({"name": "scheme.json", "mimeType": "application/json", "buffer": CONTENTS.encode()})
        self.page.wait_for_function("EQUIP_FILTER_SCHEMES_DATA[0].name === '分享方案'")
        self.assertEqual(self.schemes()[0]["id"], "first-plan")
        expect(self.page.locator("#schemeClipboardModal")).to_be_hidden()

    def test_android_bridge_supports_both_transfer_methods(self):
        self.page.evaluate("""text => {
            runtimePlatform='android'; window.androidFileChoices=0; window.androidFileExports=[];
            navigator.clipboard.readText=async () => {throw new Error('web API unavailable')};
            navigator.clipboard.writeText=async () => {throw new Error('web API unavailable')};
            window.LucimaFiles={
                readClipboardText:() => JSON.stringify({ok:true,text}),
                writeClipboardText:contents => {copiedPlans.push(contents); return '{"ok":true}'},
                chooseEquipmentFilterFile:() => {androidFileChoices++},
                saveEquipmentFilterScheme:(filename,contents) => {
                    androidFileExports.push({filename,contents}); return '{"ok":true,"path":"Downloads/方案.json"}';
                },
            };
        }""", CONTENTS)
        self.menu("export")
        self.choose("clipboard")
        self.page.wait_for_function("copiedPlans.length === 1")
        self.menu("export")
        self.choose("file")
        self.assertEqual(self.page.evaluate("androidFileExports.length"), 1)
        self.assertEqual(self.exports, [])
        self.open_clipboard()
        self.page.locator("#schemeClipboardConfirm").click()
        self.assertEqual(self.schemes()[0]["name"], "分享方案")
        self.menu("import")
        self.choose("file")
        self.assertEqual(self.page.evaluate("androidFileChoices"), 1)
        self.page.evaluate("text => onLucimaEquipmentFilterFileSelected('scheme.json',text)", CONTENTS)
        self.assertEqual(self.schemes()[0]["id"], "first-plan")

    def test_menu_keyboard_outside_click_and_scroll_close(self):
        anchor = self.menu("export")
        expect(self.page.locator('[data-scheme-transfer="clipboard"]')).to_be_focused()
        self.page.keyboard.press("ArrowDown")
        expect(self.page.locator('[data-scheme-transfer="file"]')).to_be_focused()
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#schemeTransferMenu")).to_be_hidden()
        expect(anchor).to_be_focused()
        self.menu("export")
        self.page.locator("#equipmentFilterPage .tool-page-title h2").click()
        expect(self.page.locator("#schemeTransferMenu")).to_be_hidden()
        self.menu("export")
        self.page.locator(".scheme-list").dispatch_event("scroll")
        expect(self.page.locator("#schemeTransferMenu")).to_be_visible()
        self.page.locator(".content").evaluate("e => e.scrollTop+=40")
        expect(self.page.locator("#schemeTransferMenu")).to_be_hidden()

    def test_mobile_desktop_and_short_screen_layout(self):
        for width, height in ((320, 700), (390, 844), (1280, 900), (640, 360)):
            self.page.set_viewport_size({"width": width, "height": height})
            for theme in ("dark", "light"):
                self.page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                self.menu("import")
                self.assertTrue(self.page.locator("#schemeTransferMenu").evaluate("""e => {
                    const r=e.getBoundingClientRect();
                    return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
                }"""), (width, height, theme))
                self.choose("clipboard")
                self.assertTrue(self.page.locator(".scheme-clipboard-dialog").evaluate("""e => {
                    const r=e.getBoundingClientRect();
                    return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight
                        && e.scrollWidth <= e.clientWidth;
                }"""), (width, height, theme))
                self.assertEqual(self.page.locator("#schemeClipboardInput").evaluate("e => getComputedStyle(e).fontSize"), "16px")
                self.page.locator("#schemeClipboardConfirm").scroll_into_view_if_needed()
                expect(self.page.locator("#schemeClipboardConfirm")).to_be_in_viewport()
                self.page.keyboard.press("Escape")
                expect(self.page.locator("#schemeClipboardModal")).to_be_hidden()


if __name__ == "__main__":
    unittest.main()
