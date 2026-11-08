"""Offline mobile tests of the actual sweep loops; no game requests allowed."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'frontend/app.js').read_text()


def function(name):
    start = SOURCE.index(f'function {name}(')
    return SOURCE[start:SOURCE.index('\n}', start) + 2]


class SweepSalesUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width':390, 'height':844})
        self.context.route('**/*', lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(4000)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.set_content('<html data-theme="dark"><body><main id="widgets" style="padding:12px;height:100vh;overflow:auto"></main></body></html>')
        self.page.add_style_tag(path=str(ROOT / 'frontend/style.css'))
        shared = SOURCE[SOURCE.index('const sweepSaleStates ='):SOURCE.index('// ---------- 虚构深渊')]
        activity = SOURCE[SOURCE.index('const ACT_LS ='):SOURCE.index('// ---------- 商店购买自定义控件')]
        self.page.add_script_tag(path=str(ROOT / 'frontend/action-wait.js'))
        self.page.add_script_tag(content="""
          let activeAccount = 'alpha';
          const $ = id => document.getElementById(id);
          const values={}, calls=[], logs=[], counts={hunt:0,activity:0};
          let saleMode='ok', holdSale=false, releaseSale, onBattle=null, failActivityAt=0, failHuntAt=0;
          const frontendStorageGet=key=>values[key];
          const frontendStorageSet=(key,value)=>{values[key]=value;};
          const taskIconHTML=()=>'', checkboxUIHTML=()=>'', abyssBattleHTML=()=>'', renderBattleProgress=()=>{};
          const log=(...args)=>logs.push(args), logTo=(...args)=>logs.push(args), logResult=()=>{};
          const applyStatus=()=>{}, refreshAccountStatus=()=>{};
          const waitRequestDelayBetweenIterations=async()=>{};
          const clampCount=v=>Math.min(999,Math.max(1,Number(v)||1));
          const TASKS=[{id:'hunt_sweep',name:'讨伐扫荡',desc:'选择关卡与队伍，按次数循环扫荡',sweepKind:'hunt'}, {id:'activity_scene',name:'活动关卡',desc:'选择活动关卡、队伍与助战'}];
          const rewards=(gold,powder=0)=>[{kind:'Item',id:'1',name:'金币',count:gold},
            ...['EC14','EC24','EC34','EC44','EC54','EC64'].map((id,i)=>({kind:'Item',id,name:['武器','头盔','铠甲','项链','戒指','靴子'][i]+'强化粉末',count:powder*(i+1)}))];
          const api=async(path,body)=>{
            calls.push({path,body,huntCount:counts.hunt,activityCount:counts.activity});
            if(path==='/api/sweep/equipment/sell'){
              if(holdSale)await new Promise(resolve=>{releaseSale=resolve;});
              if(saleMode==='error')throw new Error('测试网络中断');
              return {ok:saleMode!=='partial',sold:2,detail:saleMode==='partial'?'已卖出2件；第二批错误':'已卖出2件',rewardSummary:rewards(100,1)};
            }
            if(path==='/api/sweep/run'||path==='/api/activity/run'){
              const kind=path.includes('activity')?'activity':'hunt';
              counts[kind]++;
              if(onBattle)onBattle(counts[kind]);
              if(kind==='activity' && counts.activity===failActivityAt)return {ok:false,detail:'能源*24 數量不足 目前只剩:13'};
              if(kind==='hunt' && counts.hunt===failHuntAt)return {ok:false,detail:'能源不足'};
              return {ok:true,rewardSummary:rewards(10),urgentCleanup:kind==='activity'?{ok:true,results:[{ok:true,rewardSummary:[{kind:'Item',id:'catalyst',name:'催化剂',count:2}]}]}:undefined};
            }
            if(path==='/api/activity/urgent/clear')return {ok:true,results:[{ok:true,rewardSummary:rewards(50)}]};
            if(path.startsWith('/api/sweep/options'))return {elements:[{id:'fire',name:'火'}],scenes:[{id:'HuntFire_11',element:'fire',name:'关卡11'}],teams:[{id:'T',name:'队伍'}]};
            if(path==='/api/activity/options')return {scenes:[{id:'BH610_1_13',name:'活动13'}],teams:[{id:'T',name:'队伍'}],supports:[],favorites:[],activities:[]};
            return {ok:true};
          };
        """ + function('filterEscape') + shared + activity + """
          function mount(){
            $('widgets').innerHTML=renderSweepWidget(TASKS[0])+renderActivityWidget(TASKS[1]);
            bindSweepWidget('hunt_sweep');bindActivityWidget('activity_scene');
            document.querySelectorAll('.shop-body').forEach(node=>node.classList.remove('hidden'));
          }
          setSweepCfg('hunt_sweep',{sceneId:'HuntFire_11',teamId:'T',count:21});
          setActCfg({sceneId:'BH610_1_13',teamId:'T',count:21,urgentEnabled:true});
          mount();
        """)

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.context.close()

    def sales_count(self):
        return self.page.evaluate("calls.filter(c=>c.path==='/api/sweep/equipment/sell').length")

    def test_mobile_controls_align_and_totals_do_not_overflow(self):
        for prefix in ('sweep-hunt_sweep', 'act-activity_scene'):
            root = self.page.locator('#' + prefix)
            quick = root.locator('.quick-ticket-toggle').bounding_box()
            sale = root.locator('.sweep-sale-options > .target-score-toggle:first-child').bounding_box()
            self.assertGreaterEqual(sale['y'],quick['y'])
            self.assertFalse(root.locator('.sweep-sell-epic85').is_checked())
            self.assertFalse(root.locator('.sweep-auto-sell').is_checked())
            self.assertFalse(root.locator('.sweep-sale-totals').is_visible())
            self.assertEqual(root.locator('.sweep-sale-resource').count(), 7)
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        out = ROOT / 'build/sweep-sale-timing-1027'
        out.mkdir(parents=True, exist_ok=True)
        self.page.evaluate("async()=>{await loadSweepOptions('hunt_sweep');await loadActivityOptions('activity_scene');}")
        for prefix in ('sweep-hunt_sweep', 'act-activity_scene'):
            self.page.locator('#' + prefix).screenshot(path=str(out / f'{prefix}-mobile.png'))

    def test_enable_is_configuration_only_then_25_hunts_sell_at_start_10_20_and_end(self):
        self.page.locator('#sweep-hunt_sweep .sweep-sale-options > .target-score-toggle:first-child').click()
        self.assertEqual(self.sales_count(), 0)
        self.assertTrue(self.page.locator('#sweep-hunt_sweep .sweep-sale-totals').is_visible())
        self.page.evaluate("setSweepCfg('hunt_sweep',{count:25})")
        self.page.evaluate("runSweep('hunt_sweep')")
        self.assertEqual(self.sales_count(), 4)
        self.assertEqual(self.page.evaluate("calls.filter(c=>c.path==='/api/sweep/equipment/sell').map(c=>c.huntCount)"), [0,10,20,25])
        result = self.page.evaluate("({state:sweepSaleState('hunt_sweep'), final:logs.at(-1)})")
        self.assertEqual(result['state']['sold'], 8)
        self.assertIn('金币 ×650', result['final'][1])
        self.assertIn('靴子强化粉末 ×24', result['final'][1])
        self.assertIn('完成 25 把', result['final'][1])
        self.page.evaluate('mount()')
        self.assertIn('本轮贩卖 8 件', self.page.locator('#sweep-hunt_sweep .sweep-sale-totals').inner_text())
        self.page.locator('#sweep-hunt_sweep .sweep-sale-options > .target-score-toggle:first-child').click()
        self.assertFalse(self.page.locator('#sweep-hunt_sweep .sweep-sale-totals').is_visible())
        self.page.evaluate('mount()')
        self.assertFalse(self.page.locator('#sweep-hunt_sweep .sweep-sale-totals').is_visible())
        self.assertEqual(self.page.evaluate("sweepSaleState('hunt_sweep').sold"), 8)

    def test_switch_off_during_run_prevents_followup_sales(self):
        self.page.locator('#sweep-hunt_sweep .sweep-sale-options > .target-score-toggle:first-child').click()
        self.page.evaluate("""onBattle=n=>{if(n===5){const input=document.querySelector('#sweep-hunt_sweep .sweep-auto-sell');input.checked=false;input.dispatchEvent(new Event('change'));}};runSweep('hunt_sweep');""")
        self.page.wait_for_function("sweepSaleState('hunt_sweep').finished")
        self.assertEqual(self.sales_count(), 1)
        self.assertEqual(self.page.evaluate('counts.hunt'), 21)
        self.assertFalse(self.page.locator('#sweep-hunt_sweep .sweep-sale-totals').is_visible())

    def test_manual_stop_also_sells_once_at_the_end(self):
        self.page.locator('#sweep-hunt_sweep .sweep-sale-options > .target-score-toggle:first-child').click()
        self.page.evaluate("onBattle=n=>{if(n===10)requestSweepStop('hunt_sweep','alpha');};runSweep('hunt_sweep');")
        self.page.wait_for_function("sweepSaleState('hunt_sweep').finished")
        self.assertEqual(self.sales_count(), 2)
        self.assertEqual(self.page.evaluate("calls.filter(c=>c.path==='/api/sweep/equipment/sell').map(c=>c.huntCount)"), [0,10])
        self.assertIn('完成 10 把', self.page.evaluate('logs.at(-1)[1]'))
        self.assertIn('金币 ×300', self.page.evaluate('logs.at(-1)[1]'))

    def test_hunt_stamina_failure_waits_for_final_sale_before_stopping(self):
        self.page.locator('#sweep-hunt_sweep .sweep-sale-options > .target-score-toggle:first-child').click()
        self.page.evaluate("""setSweepCfg('hunt_sweep',{count:50});failHuntAt=9;
          onBattle=n=>{if(n===9)holdSale=true;};void runSweep('hunt_sweep');""")
        self.page.wait_for_function("calls.at(-1)?.path==='/api/sweep/equipment/sell' && sweepSaleState('hunt_sweep').busy")
        self.assertTrue(self.page.evaluate('sweepRunning.alpha'))
        self.assertEqual(self.page.evaluate("calls.filter(c=>c.path==='/api/sweep/run').length"),9)
        self.page.evaluate('releaseSale()')
        self.page.wait_for_function('!sweepRunning.alpha')
        self.assertIn('完成 8 把',self.page.evaluate('logs.at(-1)[1]'))

    def test_activity_collects_urgent_and_final_cleanup_income(self):
        self.page.locator('#act-activity_scene .sweep-sale-options > .target-score-toggle:first-child').click()
        self.assertEqual(self.sales_count(), 0)
        self.page.evaluate("setActCfg({count:25})")
        self.page.evaluate("runActivity('activity_scene')")
        self.assertEqual(self.sales_count(), 4)
        self.assertEqual(self.page.evaluate("calls.filter(c=>c.path==='/api/sweep/equipment/sell').map(c=>c.activityCount)"), [0,10,20,25])
        self.assertEqual(self.page.evaluate('calls.at(-2).path'), '/api/activity/urgent/clear')
        self.assertEqual(self.page.evaluate('calls.at(-1).path'), '/api/sweep/equipment/sell')
        self.assertIn('金币 ×700', self.page.evaluate('logs.at(-1)[1]'))
        self.assertIn('催化剂 ×50', self.page.evaluate('logs.at(-1)[1]'))
        self.page.locator('#act-activity_scene .sweep-sale-options > .target-score-toggle:first-child').click()
        self.assertFalse(self.page.locator('#act-activity_scene .sweep-sale-totals').is_visible())

    def test_activity_stamina_failure_after_eight_wins_waits_for_final_sale_before_stopping(self):
        self.page.locator('#act-activity_scene .sweep-sale-options > .target-score-toggle:first-child').click()
        self.page.evaluate("""setActCfg({count:50,quick:false});failActivityAt=9;
          onBattle=n=>{if(n===9)holdSale=true;};void runActivity('activity_scene');""")
        self.page.wait_for_function("calls.at(-1)?.path==='/api/sweep/equipment/sell' && sweepSaleState('activity_scene').busy")
        self.assertTrue(self.page.evaluate('actRunning.alpha'))
        self.assertFalse(self.page.evaluate("sweepSaleState('activity_scene').finished"))
        self.assertEqual(self.page.evaluate("calls.filter(c=>c.path==='/api/activity/run').length"),9)
        self.assertEqual(self.sales_count(),2)
        self.page.evaluate('releaseSale()')
        self.page.wait_for_function('!actRunning.alpha')
        self.assertIn('完成 8 把',self.page.evaluate('logs.at(-1)[1]'))
        self.assertTrue(self.page.evaluate("logs.some(row=>row[1].includes('[自动贩卖·扫荡结束]'))"))

    def test_activity_manual_stop_waits_for_final_sale_and_skips_further_battles(self):
        self.page.locator('#act-activity_scene .sweep-sale-options > .target-score-toggle:first-child').click()
        self.page.evaluate("""setActCfg({count:50});onBattle=n=>{if(n===8){
          requestActivityStop('activity_scene','alpha');holdSale=true;}};void runActivity('activity_scene');""")
        self.page.wait_for_function("calls.at(-1)?.path==='/api/sweep/equipment/sell' && sweepSaleState('activity_scene').busy")
        self.assertTrue(self.page.evaluate('actRunning.alpha'))
        self.assertEqual(self.page.evaluate("calls.filter(c=>c.path==='/api/activity/run').length"),8)
        self.assertTrue(self.page.get_by_role('button',name='停止中…',exact=True).is_disabled())
        self.page.evaluate('releaseSale()')
        self.page.wait_for_function('!actRunning.alpha')
        self.assertIn('完成 8 把',self.page.evaluate('logs.at(-1)[1]'))

    def test_failed_sale_preserves_partial_proceeds_disables_switch_and_finishes_statistics(self):
        self.page.evaluate("saleMode='partial'")
        self.page.locator('#sweep-hunt_sweep .sweep-sale-options > .target-score-toggle:first-child').click()
        self.assertEqual(self.sales_count(), 0)
        self.page.evaluate("runSweep('hunt_sweep')")
        self.page.wait_for_function("sweepSaleState('hunt_sweep').error.length>0")
        self.assertFalse(self.page.locator('#sweep-hunt_sweep .sweep-auto-sell').is_checked())
        self.assertIn('第二批错误', self.page.locator('#sweep-hunt_sweep .sweep-sale-error').inner_text())
        self.assertEqual(self.sales_count(), 1)
        self.assertIn('金币 ×310', self.page.evaluate('logs.at(-1)[1]'))

    def test_inflight_failure_is_account_and_task_scoped(self):
        self.page.evaluate("holdSale=true;saleMode='error'")
        self.page.locator('#sweep-hunt_sweep .sweep-sale-options > .target-score-toggle:first-child').click()
        self.page.evaluate("() => {void runSweep('hunt_sweep');}")
        self.page.wait_for_function('typeof releaseSale === "function"')
        self.page.evaluate("activeAccount='beta';mount();releaseSale();")
        self.page.wait_for_function("sweepSaleState('hunt_sweep','alpha').finished")
        self.assertEqual(self.page.locator('#sweep-hunt_sweep .sweep-sale-error').inner_text(), '')
        self.assertTrue(self.page.evaluate("logs.every(row=>row[0]==='alpha')"))
        self.assertFalse(self.page.evaluate("getSweepCfg('hunt_sweep','alpha').autoSell"))
        self.assertFalse(self.page.evaluate("sweepSaleState('activity_scene','alpha').initialized"))

    def test_persisted_enabled_setting_sells_before_first_battle_and_resets_next_run(self):
        self.page.evaluate("setSweepCfg('hunt_sweep',{autoSell:true});mount();")
        self.page.evaluate("runSweep('hunt_sweep')")
        self.assertEqual(self.sales_count(), 4)
        self.assertEqual(self.page.evaluate("calls[0].path"), '/api/sweep/equipment/sell')
        self.page.evaluate("runSweep('hunt_sweep')")
        self.assertEqual(self.sales_count(), 8)
        self.assertEqual(self.page.evaluate("sweepSaleState('hunt_sweep').sold"), 8)
        self.assertIn('金币 ×610', self.page.evaluate('logs.at(-1)[1]'))

    def test_disabled_setting_never_sells_but_still_reports_all_income(self):
        self.page.evaluate("runSweep('hunt_sweep')")
        self.assertEqual(self.sales_count(), 0)
        self.assertIn('金币 ×210', self.page.evaluate('logs.at(-1)[1]'))

    def test_one_run_still_sells_at_start_and_end(self):
        self.page.locator('#sweep-hunt_sweep .sweep-sale-options > .target-score-toggle:first-child').click()
        self.page.evaluate("setSweepCfg('hunt_sweep',{count:1});runSweep('hunt_sweep');")
        self.page.wait_for_function("sweepSaleState('hunt_sweep').finished")
        self.assertEqual(self.page.evaluate("calls.filter(c=>c.path==='/api/sweep/equipment/sell').map(c=>c.huntCount)"), [0,1])
        self.assertIn('金币 ×210', self.page.evaluate('logs.at(-1)[1]'))

    def test_enabling_mid_run_only_sells_at_checkpoint_or_end(self):
        self.page.evaluate("""onBattle=n=>{if(n===21){const input=document.querySelector('#sweep-hunt_sweep .sweep-auto-sell');input.checked=true;input.dispatchEvent(new Event('change'));}};runSweep('hunt_sweep');""")
        self.page.wait_for_function("sweepSaleState('hunt_sweep').finished")
        self.assertEqual(self.sales_count(), 1)
        self.assertIn('自动贩卖·扫荡结束', self.page.evaluate("logs.find(row=>row[1].includes('自动贩卖·'))[1]"))


if __name__ == '__main__':
    unittest.main()
