"""Exercise resource gauges with a controlled clock and no network access."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


class ResourceRecoveryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        source = (Path(__file__).resolve().parents[1] / "frontend/app.js").read_text(encoding="utf-8")
        cls.source = source[source.index("// 表盘资源"):source.index("function renderStatus(")]

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.route("**/*", lambda route: route.abort())
        self.page.set_content('<div id="gaugeRow"></div>')
        self.page.add_script_tag(content='''
            const $ = id => document.getElementById(id);
            const fmtNum = value => String(value);
            let activeAccount = "first", currentSection = "info";
            let now = 10000;
            Date.now = () => now;
            const ACCOUNTS = {
                first: {status:{}, connection:{connected:true}},
                second: {status:{}, connection:{connected:true}}
            };
        ''' + self.source + '''
            applyResourceGauges("first", {
                stamina: {current:30,max:60,sampledAtMs:100,recovery:{intervalMs:300000,nextInMs:1000}},
                flag: {current:1,max:5,sampledAtMs:100,recovery:{intervalMs:3600000,nextInMs:3600000}}
            });
        ''')

    def counts(self):
        return self.page.locator(".dial-val").all_text_contents()

    def test_only_a_changed_count_replaces_gauge_nodes(self):
        self.page.evaluate('window.originalGauge = $("gaugeRow").firstElementChild; now += 999; renderResourceGauges()')
        self.assertEqual(self.counts(), ["30/60", "1/5"])
        self.assertTrue(self.page.evaluate('window.originalGauge === $("gaugeRow").firstElementChild'))
        self.page.evaluate('now += 1; renderResourceGauges()')
        self.assertEqual(self.counts(), ["31/60", "1/5"])
        self.assertFalse(self.page.evaluate('window.originalGauge === $("gaugeRow").firstElementChild'))
        self.page.evaluate('now += 3 * 300000; renderResourceGauges()')
        self.assertEqual(self.counts(), ["34/60", "1/5"])
        self.assertEqual(self.page.evaluate('ACCOUNTS.first.resourceGauges.stamina.current'), 30)

    def test_hidden_page_catches_up_when_returning_and_switches_accounts(self):
        self.page.evaluate('''
            currentSection = "toolbox"; now += 3600000; renderResourceGauges();
            applyResourceGauges("second", {stamina:{current:7,max:50},flag:{current:4,max:5}});
        ''')
        self.assertEqual(self.counts(), ["30/60", "1/5"])
        self.page.evaluate('currentSection = "info"; renderResourceGauges()')
        self.assertEqual(self.counts(), ["42/60", "2/5"])
        self.page.evaluate('activeAccount = "second"; renderResourceGauges()')
        self.assertEqual(self.counts(), ["7/50", "4/5"])
        self.page.evaluate('activeAccount = "first"; renderResourceGauges()')
        self.assertEqual(self.counts(), ["42/60", "2/5"])

    def test_new_server_count_reanchors_and_delayed_response_cannot_rewind_it(self):
        self.page.evaluate('''
            now += 1000; renderResourceGauges();
            applyResourceGauges("first", {stamina:{current:20,max:60,sampledAtMs:200,
                recovery:{intervalMs:300000,nextInMs:300000}}});
            applyResourceGauges("first", {stamina:{current:30,max:60,sampledAtMs:100,
                recovery:{intervalMs:300000,nextInMs:1}}});
        ''')
        self.assertEqual(self.counts()[0], "20/60")
        self.page.evaluate('now += 300000; renderResourceGauges()')
        self.assertEqual(self.counts()[0], "21/60")

    def test_recovery_caps_and_disconnected_snapshot_stays_frozen(self):
        self.page.evaluate('now += 24 * 3600000; renderResourceGauges()')
        self.assertEqual(self.counts(), ["60/60", "5/5"])
        self.page.evaluate('''
            ACCOUNTS.first.connection.connected = false;
            applyResourceGauges("first", {stamina:{current:31,max:60,sampledAtMs:200}});
            now += 3600000; renderResourceGauges();
        ''')
        self.assertEqual(self.counts()[0], "31/60")

    def test_flag_count_can_recover_before_challenge_eligibility(self):
        source = (Path(__file__).resolve().parents[1] / "frontend/app.js").read_text(encoding="utf-8")
        self.page.add_script_tag(content='''
            const fmtClock = () => "time", fmtRemain = () => "remaining";
        ''' + source[source.index("function arenaWhen("):source.index("function renderArenaList(")])
        self.page.evaluate('''
            applyResourceGauges("first", {flag:{current:0,max:1,sampledAtMs:200,
                recovery:{intervalMs:3600000,nextInMs:1000},
                spendable:{current:0,max:1,recovery:{intervalMs:3600000,nextInMs:6000}}}});
            now += 1000; renderResourceGauges();
        ''')
        self.assertEqual(self.counts()[1], "1/1")
        expression = '''arenaWhen({waitingForFlags:true}, resourceGaugeValue(
            ACCOUNTS.first.resourceGauges.flag, now, true, true).current).txt'''
        self.assertEqual(self.page.evaluate(expression), "等待旗帜恢复")
        self.page.evaluate('now += 4999')
        self.assertEqual(self.page.evaluate(expression), "等待旗帜恢复")
        self.page.evaluate('now += 1')
        self.assertEqual(self.page.evaluate(expression), "可挑战")
        self.assertEqual(self.counts()[1], "1/1")


if __name__ == "__main__":
    unittest.main()
