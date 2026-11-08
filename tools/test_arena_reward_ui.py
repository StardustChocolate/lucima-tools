"""Real arena frontend against offline API fixtures; no game/network calls."""
from pathlib import Path
import unittest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT/'frontend/app.js').read_text()


def function(name):
    start = SOURCE.index(f'function {name}(')
    return SOURCE[start:SOURCE.index('\n}', start)+2]


class ArenaRewardUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)
        except Exception:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844},
                                                timezone_id='America/Los_Angeles')
        self.addCleanup(self.context.close)
        self.context.route('**/*', lambda route: route.fulfill(
            path=str(ROOT/'assets/items/6.png'), content_type='image/png'))
        self.page = self.context.new_page()
        self.page.set_default_timeout(4000)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.set_content('<html data-theme="dark"><head><base href="https://arena.invalid/"></head><body><main><div id="autoList"></div><div id="taskList"></div></main></body></html>')
        for name in ('style.css', 'opaque-surfaces.css'):
            self.page.add_style_tag(path=str(ROOT/'frontend'/name))
        self.page.add_style_tag(content='*{animation:none!important;transition:none!important} body{display:block;margin:0;padding:12px;height:auto;min-height:100vh;overflow:visible} main{position:relative;z-index:1;max-width:780px;margin:auto} #autoList{display:flex;flex-direction:column;gap:10px}')
        feature = SOURCE[SOURCE.index('// ---------- 竞技场周奖励（北京时间） ----------'):
                         SOURCE.index('// 单个普通任务：点自己的按钮直接执行')]
        checkbox = SOURCE[SOURCE.index('const checkboxUIHTML ='):
                          SOURCE.index('  </span>`;')+len('  </span>`;')]
        self.page.add_script_tag(path=str(ROOT / 'frontend/action-wait.js'))
        self.page.add_script_tag(content='''
          let activeAccount='alpha',NEXT_CLAIMS={},calls=[],logs=[],pending=false,release,failure='';
          const $=id=>document.getElementById(id),OPEN=Date.parse('2026-11-02T05:30:00+08:00');
          const ACCOUNTS={alpha:{params:{},nextClaims:{},toggles:{}},beta:{params:{},nextClaims:{},toggles:{}}};
          const activeAcc=()=>ACCOUNTS[activeAccount],getToggles=()=>activeAcc().toggles;
          const taskIconHTML=()=>'<img class="ticon-img" src="/assets/items/6.png" alt="">';
          const logTo=(...args)=>logs.push(args),refreshStatus=async()=>{},NEXT_WORDS={};
          const api=async(path,body)=>{
            calls.push({path,body:structuredClone(body)});
            if(path!=='/api/arena/reward/config')return {ok:true,results:[]};
            if(pending)await new Promise(resolve=>{release=resolve});
            if(failure)throw Error(failure);
            const cfg={...ACCOUNTS[body.account].params.arena_weekly_reward,...body.config,
              enabled:!!ACCOUNTS[body.account].toggles.arena_weekly_reward,nextRunAtMs:OPEN};
            return {ok:true,params:{arena_weekly_reward:cfg},arenaReward:{...cfg,nextMs:cfg.nextRunAtMs}};
          };
          const syncToggles=async()=>api('/api/toggles',{account:activeAccount,toggles:activeAcc().toggles,params:activeAcc().params});
          const runToggleTasks=async ids=>api('/api/tasks/auto',{account:activeAccount,taskIds:ids});
          const TASK={id:'arena_weekly_reward',name:'竞技场奖励',desc:'北京时间每周一 05:30–07:40 内领取周奖励，可选黄票或钻石，窗口外不补领',ui:'arena_reward'};
          Date.now=()=>OPEN-1000;
        '''+checkbox+function('filterEscape')+function('fmtClock')+function('fmtRemain')+
            function('fmtNextClaim')+feature+function('onToggleChange')+function('renderNextClaims')+
            function('renderAutoPanel')+function('renderManualPanel')+'''
          function mount(){NEXT_CLAIMS=ACCOUNTS[activeAccount].nextClaims;renderAutoPanel([TASK]);renderManualPanel([])} mount();
        ''')
        self.assertEqual(self.errors, [])
        self.page.get_by_role('button', name='选项 ▸', exact=True).click()

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def toggle_enabled(self):
        self.page.locator('#auto-arena_weekly_reward .switch-wrap').click()

    def saved(self):
        self.page.wait_for_function("document.querySelector('.arena-reward-feedback').textContent==='已自动保存'")

    def set_receipt(self, items="[{itemStaticId:'6',name:'黄票（神秘契约）',count:42}]"):
        self.page.evaluate("""() => {
          const state={enabled:true,rewardItem:'2',nextMs:OPEN+7*86400000,
            lastReward:{claimedAtMs:OPEN,requestedItem:'6',items:ITEMS}};
          ACCOUNTS.alpha.params.arena_weekly_reward=state;
          ACCOUNTS.alpha.nextClaims.arena_weekly_reward=state;
          NEXT_CLAIMS=ACCOUNTS.alpha.nextClaims;renderArenaRewardStatus();
        }""".replace('ITEMS', items))

    def test_defaults_exclusive_rewards_no_time_controls_and_exact_warning(self):
        self.assertEqual(self.page.locator('#taskList .arena-reward-widget').count(), 0)
        self.assertEqual(self.page.locator('.arena-reward-weekday, .arena-reward-time, input[type="time"]').count(), 0)
        self.assertTrue(self.page.locator('input[value="6"]').is_checked())
        self.assertFalse(self.page.locator('#auto-arena_weekly_reward .switch-in').is_checked())
        self.assertTrue(self.page.get_by_text('该功能因无法测试，使用后需要自己检查一下', exact=True).is_visible())
        options_text = self.page.locator('.arena-reward-body').inner_text()
        self.assertNotIn('固定领取窗口', options_text)
        self.assertNotIn('每周一', options_text)
        self.assertNotIn('05:30', options_text)
        self.assertTrue(options_text.startswith('领取奖励（二选一）'))
        self.assertIn('设置自动保存；', options_text)
        self.assertIn('窗口外不补领', self.page.locator('.arena-reward-body').inner_text())
        self.assertEqual(self.page.evaluate('calls'), [])
        self.page.locator('input[value="2"]').locator('..').click()
        self.saved()
        self.assertFalse(self.page.locator('input[value="6"]').is_checked())
        self.assertFalse(self.page.locator('#auto-arena_weekly_reward .switch-in').is_checked())
        self.assertEqual(self.page.evaluate('calls.map(row=>row.path)'), ['/api/arena/reward/config'])
        self.assertEqual(self.page.evaluate('calls[0].body.config'), {'rewardItem': '2'})

    def test_save_survives_account_switch_without_any_time_payload(self):
        self.toggle_enabled()
        self.page.locator('input[value="2"]').locator('..').click()
        self.saved()
        body = self.page.evaluate("calls.filter(row=>row.path==='/api/arena/reward/config').at(-1).body")
        self.assertEqual(body, {'account': 'alpha', 'config': {'rewardItem': '2'}})
        self.assertTrue(self.page.evaluate("calls.some(row=>row.path==='/api/toggles'&&row.body.toggles.arena_weekly_reward)"))
        self.page.evaluate('mount()')
        self.assertTrue(self.page.locator('input[value="2"]').is_checked())
        self.page.evaluate("activeAccount='beta';mount()")
        self.assertTrue(self.page.locator('input[value="6"]').is_checked())
        self.assertFalse(self.page.locator('#auto-arena_weekly_reward .switch-in').is_checked())

    def test_header_toggle_beijing_schedule_ignores_browser_timezone(self):
        self.toggle_enabled()
        self.page.locator('input[value="2"]').locator('..').click()
        self.saved()
        text = self.page.locator('.arena-reward-status').inner_text()
        self.assertTrue(text.startswith('已开启 · 钻石 · 下次：'))
        self.assertNotIn('每周一', text)
        self.assertNotIn('07:40', text)
        self.assertNotIn('· ·', text)
        self.assertIn('下次：11/02 05:30', text)
        self.toggle_enabled()
        self.page.evaluate('renderArenaRewardStatus()')
        self.assertIn('未开启', self.page.locator('.arena-reward-status').inner_text())

    def test_success_time_actual_kind_and_quantity_visible_even_when_options_collapsed(self):
        self.set_receipt()
        self.page.get_by_role('button', name='选项 ▾', exact=True).click()
        status = self.page.locator('.arena-reward-status')
        self.assertTrue(status.is_visible())
        text = status.inner_text()
        self.assertIn('2026/11/02 05:30:00（北京时间）', text)
        self.assertIn('实际奖励：黄票（神秘契约） ×42', text)
        self.assertIn('· 钻石', text)  # Current selection does not relabel a historical receipt.

    def test_missing_quantity_and_missing_details_explicitly_visible(self):
        self.set_receipt("[{itemStaticId:'2',name:'钻石',count:null}]")
        self.assertIn('实际奖励：钻石 （未返回数量）', self.page.locator('.arena-reward-status').inner_text())
        self.set_receipt('[]')
        text = self.page.locator('.arena-reward-status').inner_text()
        self.assertIn('未返回财物明细（本次选择：黄票），未返回数量', text)
        self.assertNotIn('×0', text)

    def test_legacy_completed_and_already_claimed_do_not_show_new_success(self):
        self.page.evaluate("""() => {
          ACCOUNTS.alpha.params.arena_weekly_reward={weekday:7,hour:23,minute:59,
            lastCompletedAtMs:OPEN,lastResult:'竞技场本期周奖励已领取'};
          renderArenaRewardStatus();
        }""")
        text = self.page.locator('.arena-reward-status').inner_text()
        self.assertIn('暂无本工具成功领取记录', text)
        self.assertIn('最近检查：竞技场本期周奖励已领取', text)
        self.assertNotIn('每周一', text)
        self.assertNotIn('23:59', text)
        self.assertNotIn('2026/11/02', text)
        self.set_receipt()
        self.page.evaluate("NEXT_CLAIMS.arena_weekly_reward.lastResult='竞技场本期周奖励已领取';renderArenaRewardStatus()")
        self.assertIn('黄票（神秘契约） ×42', self.page.locator('.arena-reward-status').inner_text())

    def test_receipts_are_isolated_on_account_switch(self):
        self.set_receipt()
        self.page.evaluate("activeAccount='beta';mount()")
        text = self.page.locator('.arena-reward-status').inner_text()
        self.assertIn('暂无本工具成功领取记录', text)
        self.assertNotIn('×42', text)
        self.page.evaluate("activeAccount='alpha';mount()")
        self.assertIn('×42', self.page.locator('.arena-reward-status').inner_text())

    def test_late_save_never_changes_another_account(self):
        self.page.evaluate('pending=true')
        self.page.locator('input[value="2"]').locator('..').click()
        self.page.wait_for_function("typeof release==='function'")
        self.page.evaluate("activeAccount='beta';mount();release()")
        self.page.wait_for_function("ACCOUNTS.alpha.params.arena_weekly_reward?.rewardItem==='2'")
        self.assertTrue(self.page.locator('input[value="6"]').is_checked())
        self.assertEqual(self.page.evaluate('ACCOUNTS.beta.params'), {})

    def test_failed_save_can_retry_by_changing_reward(self):
        self.page.evaluate("failure='离线'")
        self.page.locator('input[value="2"]').locator('..').click()
        self.page.wait_for_function("document.querySelector('.arena-reward-feedback').textContent.includes('保存失败')")
        self.assertEqual(self.page.evaluate('ACCOUNTS.alpha.params'), {})
        self.page.evaluate("failure=''")
        self.page.locator('input[value="6"]').locator('..').click()
        self.saved()
        self.assertEqual(self.page.evaluate('ACCOUNTS.alpha.params.arena_weekly_reward.rewardItem'), '6')

    def test_retry_and_next_week_status_use_backend_deadlines(self):
        self.page.evaluate("""() => {
          ACCOUNTS.alpha.toggles.arena_weekly_reward=true;
          NEXT_CLAIMS.arena_weekly_reward={nextMs:OPEN+300000,retrying:true};renderArenaRewardStatus();
        }""")
        self.assertIn('失败重试：11/02 05:35', self.page.locator('.arena-reward-status').inner_text())
        self.page.evaluate("NEXT_CLAIMS.arena_weekly_reward={nextMs:OPEN+7*86400000,retrying:false};renderArenaRewardStatus()")
        self.assertIn('下次：11/09 05:30', self.page.locator('.arena-reward-status').inner_text())

    def test_mobile_desktop_themes_and_long_receipts_have_no_overflow(self):
        self.set_receipt("[{itemStaticId:'6',name:'黄票（神秘契约）',count:42},{itemStaticId:'2',name:'钻石',count:null}]")
        folder = ROOT/'build/arena-reward-preview'
        folder.mkdir(parents=True, exist_ok=True)
        for width, theme in ((360, 'dark'), (390, 'dark'), (390, 'light'), (1280, 'light')):
            self.page.set_viewport_size({'width': width, 'height': 844})
            self.page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            self.assertTrue(self.page.get_by_text('该功能因无法测试，使用后需要自己检查一下', exact=True).is_visible())
            if (width, theme) in ((390, 'dark'), (1280, 'light')):
                self.page.screenshot(path=str(folder/f'{width}-{theme}.png'), full_page=True)


if __name__ == '__main__':
    unittest.main()
