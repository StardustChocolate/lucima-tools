"""Real module teardown, native controls and bounded page/listener growth."""
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from tools import test_account_switch_ui as fixtures

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / 'build/module-lifecycle-review'


class ModuleLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.AccountSwitchUI.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.AccountSwitchUI.tearDownClass.__func__(cls)

    setUp = fixtures.AccountSwitchUI.setUp
    tearDown = fixtures.AccountSwitchUI.tearDown

    def serve(self, route):
        path = urlsplit(route.request.url).path
        if path in ('/api/gvg/catalog', '/api/gvg/action'):
            value = {'characters': [], 'artifacts': [], 'versions': {}} if path.endswith('catalog') else {
                'roles': [], 'snapshotHash': 'offline'}
            route.fulfill(content_type='application/json', body=json.dumps(value))
            return
        fixtures.AccountSwitchUI.serve(self, route)

    def test_native_auto_task_checkboxes_share_the_right_edge_with_option_cards(self):
        for width in (320,390,760,1280):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.page.evaluate("showSection('auto')")
            rows=self.page.locator('#autoList .auto-item input.switch-in').evaluate_all("""inputs=>inputs.map(input=>{
              const row=input.closest('.auto-item'),box=input.getBoundingClientRect(),card=row.getBoundingClientRect();
              return {id:row.dataset.id,right:card.right-box.right,width:box.width};
            })""")
            self.assertTrue(len(rows)>5)
            self.assertLessEqual(max(r['right'] for r in rows)-min(r['right'] for r in rows),1,(width,rows))
            self.assertTrue(all(r['width']>=18 for r in rows),(width,rows))

    def test_every_inactive_module_releases_its_content_and_is_usable_after_remount(self):
        sections = ['info', 'auto', 'manual', 'guild', 'equipment', 'gvg', 'toolbox', 'logs', 'settings', 'about']
        for _ in range(3):
            for section in sections:
                self.page.evaluate('id => showSection(id)', section)
                self.page.wait_for_timeout(20)
                result = self.page.evaluate("""() => ({
                  mounted:[...document.querySelectorAll('.content > .section')].filter(n=>n.childElementCount).map(n=>n.id),
                  inactiveImages:document.querySelectorAll('.content > .section:not(.active) img').length,
                  controllers:sectionControllers.size
                })""")
                self.assertEqual(result['mounted'], ['section-' + section], result)
                self.assertEqual(result['inactiveImages'], 0)
                self.assertEqual(result['controllers'], 1)
        self.page.evaluate("showSection('settings')")
        self.page.locator('#accentPick').fill('#445566')
        self.assertEqual(self.page.evaluate('getTheme().accent'), '#445566')
        self.page.evaluate("showSection('manual')")
        self.page.locator('.intimacy-start').click()
        self.assertTrue(self.page.locator('.intimacy-body').is_visible())

    def test_switching_away_from_a_running_farm_keeps_only_state_and_does_not_restart_it(self):
        self.page.evaluate("showSection('manual')")
        before = len([row for row in self.calls if row[0] == '/api/intimacy/start'])
        self.page.evaluate("showSection('info')")
        self.assertEqual(self.page.locator('#section-manual *').count(), 0)
        self.page.evaluate("""() => {
          applyStatus('alpha',{name:'刷新后的账号'},{},null,null,null);
          applyIntimacy('alpha',{running:true,revision:99,progress:{phase:'battle',teams:{upper:[{
            roleId:'r',staticId:'H026',name:'卡洛琳',intimacyLevel:5,intimacyExp:45,intimacyMax:150}]}}});
        }""")
        self.assertEqual(self.page.locator('#section-manual *').count(), 0)
        self.page.evaluate("showSection('manual')")
        self.assertIn('45/150', self.page.locator('.intimacy-body').inner_text())
        self.assertEqual(len([row for row in self.calls if row[0] == '/api/intimacy/start']), before)

    def test_mobile_task_inputs_use_native_checkboxes_and_selects(self):
        self.page.evaluate("showSection('manual'); setActCfg({mode:'progress',family:'BH806',teamId:'T'});")
        self.page.evaluate("""() => {
          const slot=document.querySelector('.act-family-slot');
          slot.innerHTML=dropdownHTML('act-family',[{val:'BH806',label:'活动 · 已通关 0/13'},{val:'BH610',label:'其他活动 · 已通关 0/13'}],'BH806');
          bindDropdown(slot.querySelector('.dropdown'),value=>setActCfg({family:value}));
          document.querySelector('#act-activity_scene .shop-body').classList.remove('hidden');
          updateActivityMode('activity_scene');
        }""")
        self.page.locator('.act-family select').select_option('BH610')
        self.assertEqual(self.page.evaluate('getActCfg().family'), 'BH610')
        self.assertEqual(self.page.locator('.act-family .dd-menu').count(), 0)
        input = self.page.locator('.act-ticket-sales .sweep-auto-sell')
        self.assertEqual(input.evaluate('n=>getComputedStyle(n).position'), 'static')
        self.assertGreaterEqual(input.bounding_box()['width'], 18)
        self.assertTrue(self.page.locator('.act-ticket-sales .sweep-sell-epic85').is_visible())
        self.page.locator('#act-activity_scene').screenshot(path=str(REVIEW / 'activity-progress-native-controls.png'))

    def test_repeated_navigation_has_bounded_dom_and_event_listener_counts(self):
        self.page.evaluate("showSection('manual')")
        session = self.context.new_cdp_session(self.page)
        self.page.wait_for_timeout(300)
        session.send('HeapProfiler.collectGarbage')
        before = session.send('Memory.getDOMCounters')
        durations = self.page.evaluate("""() => {
          const samples=[];
          for(let i=0;i<100;i++){
            const start=performance.now();
            showSection('info');showSection('auto');showSection('manual');
            samples.push(performance.now()-start);
          }
          return samples;
        }""")
        self.page.wait_for_timeout(500)
        session.send('HeapProfiler.collectGarbage')
        after = session.send('Memory.getDOMCounters')
        report = {'before': before, 'after': after, 'navigationCount': 300,
                  'millisecondsPerThreeSwitches': {'median': sorted(durations)[50], 'max': max(durations)}}
        (REVIEW / 'navigation-performance.json').write_text(json.dumps(report, indent=2))
        self.assertLessEqual(after['nodes'], before['nodes'] + 80, report)
        self.assertLessEqual(after['jsEventListeners'], before['jsEventListeners'] + 20, report)


if __name__ == '__main__':
    unittest.main()
