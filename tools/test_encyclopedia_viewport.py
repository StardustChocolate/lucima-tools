"""Real-browser regressions for the archive viewer's scrollbar/scale feedback."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


class EncyclopediaViewportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
        app = (ROOT / "frontend/app.js").read_text(encoding="utf-8")
        # Use the production viewer and its observers; the card only needs a
        # controllable natural height to exercise the scrollbar threshold.
        cls.viewer = app[app.index("function encyclopediaArchiveViewState()"):
                         app.index("function renderEncyclopediaCard(")]
        cls.playwright = sync_playwright().start()
        try:
            # Playwright normally hides scrollbars, which masks this defect.
            cls.browser = cls.playwright.chromium.launch(
                headless=True, ignore_default_args=["--hide-scrollbars"])
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
        finally:
            cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def mount(self, *, old_webview=False, overlay=False):
        css = self.css
        if old_webview:
            css = css.replace("scrollbar-gutter", "unsupported-scrollbar-gutter")
        self.page.set_content("""<!doctype html><meta name="viewport" content="width=device-width">
            <body style="padding:10px">
              <div class="encyclopedia-archive-viewport">
                <div class="encyclopedia-archive-stage">
                  <div class="encyclopedia-archive-shell">
                    <div class="encyclopedia-archive-scale">
                      <article class="archive-card" style="width:1000px;height:1200px"></article>
                    </div>
                  </div>
                </div>
              </div>
              <span id="encyclopediaZoomValue"></span>
            </body>""")
        self.page.add_style_tag(content=css)
        if overlay:
            # Overlay scrollbars consume no layout space, as on Android.
            self.page.add_style_tag(content=".encyclopedia-archive-viewport {scrollbar-width:none}")
        self.page.add_script_tag(content="""
            const ENCYCLOPEDIA_STATE = {archiveView: {zoomFactor: 1, scale: 1, fitScale: 1}};
            const $ = id => document.getElementById(id);
        """ + self.viewer)
        self.page.evaluate("initEncyclopediaArchiveViewport(document.querySelector('.encyclopedia-archive-viewport'))")

    def threshold_height(self):
        return self.page.evaluate("""() => {
            const viewport = encyclopediaArchiveViewState().viewport;
            return Math.floor(viewport.offsetHeight * 1000 / viewport.offsetWidth) + 15;
        }""")

    def sample(self, height=None):
        return self.page.evaluate("""async height => {
            const v = encyclopediaArchiveViewState();
            if (height !== null) v.card.style.height = `${height}px`;
            const rows = [];
            for (let i = 0; i < 32; i++) {
                await new Promise(requestAnimationFrame);
                rows.push({scale: v.scale, zoom: v.zoomFactor,
                    width: v.viewport.clientWidth, height: v.viewport.clientHeight,
                    scrollWidth: v.viewport.scrollWidth, scrollHeight: v.viewport.scrollHeight,
                    cardWidth: v.card.getBoundingClientRect().width,
                    cardHeight: v.card.getBoundingClientRect().height,
                    gutter: v.viewport.offsetWidth - v.viewport.clientWidth});
            }
            return rows.slice(8);
        }""", height)

    def assert_stable(self, rows, *, fit=True):
        self.assertEqual(len({tuple(row.values()) for row in rows}), 1,
                         f"Viewer keeps changing without input: {rows[-6:]}")
        state = rows[-1]
        if fit:
            self.assertLessEqual(state["scrollWidth"] - state["width"], 1)
            self.assertLessEqual(state["cardWidth"], state["width"] + 1)
        return state

    def test_threshold_card_stops_toggling_scrollbars_at_phone_sizes(self):
        self.mount()
        for width, height in ((320, 740), (390, 844), (412, 915)):
            with self.subTest(width=width, height=height):
                self.page.set_viewport_size({"width": width, "height": height})
                state = self.assert_stable(self.sample(self.threshold_height()))
                self.assertGreater(state["gutter"], 0, "Test must use space-consuming scrollbars")

    def test_short_and_long_cards_keep_the_same_fit_width_and_scroll_when_needed(self):
        self.mount()
        short = self.assert_stable(self.sample(1200))
        long = self.assert_stable(self.sample(2300))
        self.page.evaluate("encyclopediaArchiveViewState().viewport.scrollTop = 10000")
        self.assertEqual(self.page.evaluate("encyclopediaArchiveViewState().viewport.scrollTop"),
                         long["scrollHeight"] - long["height"])
        short_again = self.assert_stable(self.sample(1200))
        self.assertEqual(short["scrollHeight"], short["height"])
        self.assertGreater(long["scrollHeight"], long["height"])
        self.assertEqual(short["scale"], long["scale"])
        self.assertEqual(short, short_again)
        self.page.evaluate("encyclopediaArchiveViewState().viewport.scrollTop = 10000")
        self.assertEqual(self.page.evaluate("encyclopediaArchiveViewState().viewport.scrollTop"), 0)

    def test_zoom_reset_and_orientation_keep_layout_stable(self):
        self.mount()
        before = self.assert_stable(self.sample(self.threshold_height()))
        self.page.evaluate("setEncyclopediaZoom('in')")
        zoomed = self.assert_stable(self.sample(), fit=False)
        self.assertGreater(zoomed["scrollWidth"], zoomed["width"])
        self.assertGreater(zoomed["scale"], before["scale"])
        self.page.set_viewport_size({"width": 844, "height": 390})
        landscape = self.assert_stable(self.sample(), fit=False)
        self.assertEqual(landscape["zoom"], zoomed["zoom"])
        self.page.evaluate("setEncyclopediaZoom('reset')")
        self.assertEqual(self.assert_stable(self.sample())["zoom"], 1)
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertEqual(self.assert_stable(self.sample()), before)

    def test_old_webview_fallback_has_a_stable_gutter(self):
        self.mount(old_webview=True)
        state = self.assert_stable(self.sample(self.threshold_height()))
        self.assertGreater(state["gutter"], 0)
        self.assert_stable(self.sample(2300))
        self.assert_stable(self.sample(1200))

    def test_overlay_scrollbars_still_use_the_full_available_width(self):
        self.mount(overlay=True)
        state = self.assert_stable(self.sample(self.threshold_height()))
        self.assertEqual(state["gutter"], 0)
        self.assertAlmostEqual(state["cardWidth"], state["width"], delta=0.01)
        self.assert_stable(self.sample(2300))


if __name__ == "__main__":
    unittest.main()
