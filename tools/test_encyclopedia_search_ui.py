"""Search and filter interaction contracts for the cloud encyclopedia."""
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class EncyclopediaSearchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        app = (ROOT / "frontend/app.js").read_text(encoding="utf-8")
        state = app[app.index("const ENCYCLOPEDIA_STATE ="):app.index("let encyclopediaIndexPending =")]
        filters = app[app.index("function characterMatchesFilter("):app.index("function characterStatusIconsHTML(")]
        grid = app[app.index("function encyclopediaFilteredIndex()"):app.index("function setEncyclopediaMode(")]
        cls.script = "\n".join((
            "const $ = id => document.getElementById(id);",
            "function encyclopediaEscape(value) { return String(value ?? '').replace(/[&<>\\\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\\\"':'&quot;',\"'\":'&#39;'}[c])); }",
            state,
            "let encyclopediaSearchTimer = null;",
            filters,
            grid,
        ))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.set_content("""
          <input id="encyclopediaSearchInput"><button id="encyclopediaSearchClear" hidden></button>
          <div id="encyclopediaGrid"></div><div id="encyclopediaEmpty" class="hidden"></div>
          <span id="encyclopediaResultCount"></span><span id="encyclopediaTotalCount"></span>
        """)
        self.page.add_script_tag(content=self.script)
        self.page.evaluate("""() => {
          ENCYCLOPEDIA_STATE.index = [
            {id: 'H001', name: '夏妮', nameEn: 'Shani', nameE7: '塞西莉亚', aliases: ['火龙'], element: 'Fire', job: 'Defender', star: 5},
            {id: 'H002', name: '菲莉斯', nameEn: 'Phyllis', nameE7: '', aliases: ['火隆'], element: 'Ice', job: 'Medic', star: 4},
            {id: 'H003', name: '玖可', nameEn: 'Joki', nameE7: '', aliases: [], element: 'Earth', job: 'Sniper', star: 3},
          ];
          ENCYCLOPEDIA_STATE.search = {
            ids: {h001: ['H001'], h002: ['H002'], h003: ['H003']},
            names: {夏妮: ['H001'], shani: ['H001'], 菲莉斯: ['H002'], phyllis: ['H002'], 玖可: ['H003'], joki: ['H003']},
            aliases: {塞西莉亚: ['H001'], 火龙: ['H001'], 火隆: ['H002']},
            pinyin: {xiani: ['H001'], shani: ['H001'], huolong: ['H001', 'H002'], feilisi: ['H002'], jiuke: ['H003'], joki: ['H003']},
          };
          renderEncyclopediaGrid();
        }""")

    def test_pinyin_search_combines_with_existing_filters(self):
        result = self.page.evaluate("""() => {
          ENCYCLOPEDIA_STATE.searchQuery = 'xia ni';
          ENCYCLOPEDIA_STATE.filters.job = ['Defender'];
          renderEncyclopediaGrid();
          return {
            ids: [...document.querySelectorAll('[data-encyclopedia-character]')].map(node => node.dataset.encyclopediaCharacter),
            count: document.getElementById('encyclopediaResultCount').textContent,
          };
        }""")
        self.assertEqual(result, {"ids": ["H001"], "count": "1"})

        collisions = self.page.evaluate("""() => {
          ENCYCLOPEDIA_STATE.searchQuery = 'huolong';
          ENCYCLOPEDIA_STATE.filters.job = [];
          renderEncyclopediaGrid();
          return [...document.querySelectorAll('[data-encyclopedia-character]')].map(node => node.dataset.encyclopediaCharacter);
        }""")
        self.assertEqual(collisions, ["H001", "H002"])

    def test_older_index_falls_back_to_names_and_aliases(self):
        result = self.page.evaluate("""() => {
          ENCYCLOPEDIA_STATE.search = null;
          ENCYCLOPEDIA_STATE.searchQuery = '火龙';
          renderEncyclopediaGrid();
          const alias = [...document.querySelectorAll('[data-encyclopedia-character]')].map(node => node.dataset.encyclopediaCharacter);
          ENCYCLOPEDIA_STATE.searchQuery = 'xiani';
          renderEncyclopediaGrid();
          const pinyin = [...document.querySelectorAll('[data-encyclopedia-character]')].map(node => node.dataset.encyclopediaCharacter);
          return {alias, pinyin};
        }""")
        self.assertEqual(result, {"alias": ["H001"], "pinyin": []})

    def test_search_input_updates_results_and_clear_restores_list(self):
        result = self.page.evaluate("""async () => {
          bindEncyclopediaSearch();
          const input = document.getElementById('encyclopediaSearchInput');
          const clear = document.getElementById('encyclopediaSearchClear');
          input.value = 'H003'; input.dispatchEvent(new Event('input'));
          await new Promise(resolve => setTimeout(resolve, 150));
          const filtered = [...document.querySelectorAll('[data-encyclopedia-character]')].map(node => node.dataset.encyclopediaCharacter);
          const filteredCount = document.getElementById('encyclopediaResultCount').textContent;
          const clearShown = !clear.hidden;
          clear.click();
          const restored = [...document.querySelectorAll('[data-encyclopedia-character]')].map(node => node.dataset.encyclopediaCharacter);
          const restoredCount = document.getElementById('encyclopediaResultCount').textContent;
          return {filtered, filteredCount, clearShown, restored, restoredCount, clearHidden: clear.hidden};
        }""")
        self.assertEqual(result, {
            "filtered": ["H003"], "filteredCount": "1", "clearShown": True,
            "restored": ["H001", "H002", "H003"], "restoredCount": "3", "clearHidden": True,
        })

    def test_search_and_counts_share_a_stable_row_on_mobile_and_desktop(self):
        source = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        start = source.index('<div class="encyclopedia-search-row">')
        end = source.index('<div class="encyclopedia-filter-group">', start)
        header = source[start:end]
        self.page.set_content('<html data-theme="dark"><body><main style="padding:12px">'
                              '<div class="encyclopedia-filter-panel">' + header + '</div></main></body></html>')
        self.page.add_style_tag(path=str(ROOT / "frontend/style.css"))
        for width in (320, 390, 1280):
            for theme in ("dark", "light"):
                with self.subTest(width=width, theme=theme):
                    self.page.set_viewport_size({"width": width, "height": 720})
                    result = self.page.evaluate('''theme => {
                      document.documentElement.dataset.theme = theme;
                      const input = document.getElementById("encyclopediaSearchInput");
                      const count = document.querySelector(".encyclopedia-result-count");
                      document.getElementById("encyclopediaResultCount").textContent = "1";
                      document.getElementById("encyclopediaTotalCount").textContent = "227";
                      const before = input.getBoundingClientRect();
                      document.getElementById("encyclopediaResultCount").textContent = "227";
                      const after = input.getBoundingClientRect(), counts = count.getBoundingClientRect();
                      return {width: after.width, unchanged: before.width === after.width,
                        sameRow: Math.abs((after.top + after.height / 2) - (counts.top + counts.height / 2)) < 1,
                        fits: counts.right <= innerWidth && document.documentElement.scrollWidth === innerWidth};
                    }''', theme)
                    self.assertGreater(result["width"], 100)
                    self.assertTrue(result["unchanged"])
                    self.assertTrue(result["sameRow"])
                    self.assertTrue(result["fits"])


if __name__ == "__main__":
    unittest.main()
