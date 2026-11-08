"""Mobile friendship controls against deterministic offline API snapshots."""
from pathlib import Path
import re
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'frontend/app.js').read_text()
REVIEW = ROOT / 'build/intimacy-half-retry-review'


def function(name):
    start = SOURCE.index(f'function {name}(')
    return SOURCE[start:SOURCE.index('\n}', start) + 2]


class FriendshipUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        cls.browser = cls.playwright.chromium.launch(headless=True, **({'executable_path': str(chrome)} if chrome.is_file() else {}))
        REVIEW.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=1)
        self.context.route('**/*', lambda route: route.abort())
        self.context.route('**/assets/avatars/*.png', lambda route: route.fulfill(
            body=(ROOT / 'assets/avatars' / route.request.url.rsplit('/', 1)[-1]).read_bytes(), content_type='image/png'))
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.set_content('<html data-theme="dark"><head><base href="https://intimacy.invalid/"></head><body><main id="widgets"></main></body></html>')
        self.page.add_style_tag(path=str(ROOT / 'frontend/style.css'))
        self.page.add_style_tag(content='* {animation:none!important; transition:none!important;} body {display:block; margin:0; padding:16px; height:auto; min-height:100vh; overflow:visible;} #widgets {position:relative; z-index:1; max-width:750px; margin:auto; display:flex; flex-direction:column; gap:10px;}')
        icon = re.search(r'<symbol id="nav-heart".*?</symbol>', (ROOT / 'frontend/index.html').read_text(), re.S).group(0)
        self.page.evaluate('(icon) => document.body.insertAdjacentHTML("afterbegin", `<svg width="0" height="0" aria-hidden="true">${icon}</svg>`)', icon)
        feature = SOURCE[SOURCE.index('// ---------- 自动刷亲密度 ----------'):SOURCE.index('// ---------- 虚构深渊：两波自由配队 ----------')]
        self.page.add_script_tag(path=str(ROOT / 'frontend/action-wait.js'))
        self.page.add_script_tag(content='''
          let activeAccount = 'alpha';
          const $ = id => document.getElementById(id);
          const calls = [], logs = [];
          const TASK = {id:'intimacy_auto',name:'自动刷亲密度',desc:'自动刷满所有初始四五星角色亲密度到10'};
          const logTo = (...args) => logs.push(args);
          const applyStatus = () => {};
          const DATA = {running:true, progress:{phase:'progress',message:'本轮亲密度已同步，后续固定这两名主输出',
            rounds:3, attempts:4, carriersLocked:true, total:90, completed:70, remaining:20,
            teams:{upper:[{roleId:'a',staticId:'H002',name:'菲莉斯',carrier:true,intimacyLevel:10,intimacyExp:0},
              {roleId:'b',staticId:'H026',name:'卡洛琳',intimacyLevel:7,intimacyExp:65,intimacyMax:250},
              {roleId:'c',staticId:'H027',name:'奥普怀妮',intimacyLevel:7,intimacyExp:6,intimacyMax:250},
              {roleId:'d',staticId:'H028',name:'吉赛尔',intimacyLevel:6,intimacyExp:153,intimacyMax:200}],
            lower:[{roleId:'e',staticId:'H006',name:'璐茜娅',carrier:true,intimacyLevel:10,intimacyExp:0},
              {roleId:'f',staticId:'H029',name:'乔依丝',intimacyLevel:6,intimacyExp:30,intimacyMax:200},
              {roleId:'g',staticId:'H030',name:'茱莉亚',intimacyLevel:5,intimacyExp:116,intimacyMax:150},
              {roleId:'h',staticId:'H031',name:'玛蒂尔达',intimacyLevel:5,intimacyExp:104,intimacyMax:150}]}}};
          for(const team of Object.values(DATA.progress.teams)) for(const row of team) row.avatar = `/assets/avatars/${row.staticId}.png`;
          let startFailure = '', startPending = false, releaseStart;
          const api = async (path, body) => {
            calls.push({path,body});
            if(path==='/api/intimacy/start') {
              if(startFailure) throw Error(startFailure);
              if(startPending) await new Promise(resolve => {releaseStart = resolve;});
              return {ok:true,intimacy:structuredClone(DATA)};
            }
            if(path==='/api/intimacy/cancel') return {ok:true,intimacy:structuredClone(DATA)};
            if(path==='/api/abyss/restore') return {ok:true,detail:'原装备与羁绊已归还'};
            throw Error('unexpected request');
          };
        ''' + function('filterEscape') + feature + '''
          function mount() {
            $('widgets').innerHTML = '<div class="shop-widget"><div class="shop-head"><div class="ticon">◇</div><div class="tinfo"><div class="tname">虚构深渊</div><div class="tdesc">配置队伍，自动进行战斗</div></div><button class="mini">选项 ▸</button></div></div>' + renderIntimacyWidget(TASK);
            bindIntimacyWidget(TASK.id);
          }
          mount();
        ''')

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.context.close()

    def test_start_expands_actual_server_progress_and_cancel_waits_for_restoration(self):
        self.assertTrue(self.page.locator('.intimacy-body').evaluate("el => el.classList.contains('hidden')"))
        self.page.get_by_role('button', name='启动', exact=True).click()
        self.page.get_by_role('button', name='中止', exact=True).wait_for()
        self.assertEqual(self.page.locator('.intimacy-member').count(), 6)
        self.assertEqual(self.page.locator('.intimacy-member.carrier').count(), 0)
        self.assertEqual(self.page.locator('.intimacy-members').count(), 1)
        self.assertIn('亲密度7', self.page.locator('.intimacy-body').inner_text())
        self.assertIn('65/250', self.page.locator('.intimacy-body').inner_text())
        self.assertIn('153/200', self.page.locator('.intimacy-body').inner_text())
        self.assertIn('116/150', self.page.locator('.intimacy-body').inner_text())
        self.assertNotIn('进度', self.page.locator('.intimacy-body').inner_text())
        self.assertNotIn('主输出已固定', self.page.locator('.intimacy-body').inner_text())
        self.assertNotIn('本轮亲密度已同步', self.page.locator('.intimacy-body').inner_text())
        self.assertNotIn('已完成', self.page.locator('.intimacy-body').inner_text())
        self.assertEqual(self.page.locator('.intimacy-body h4').count(), 0)
        self.assertEqual(self.page.locator('.intimacy-member[aria-label="菲莉斯"]').count(), 0)
        self.assertEqual(self.page.locator('.intimacy-member[aria-label="璐茜娅"]').count(), 0)
        self.assertNotIn('陪刷角色', self.page.locator('.intimacy-body').inner_text())
        self.assertNotIn('卡洛琳', self.page.locator('.intimacy-body').inner_text())
        portrait_widths = []
        for width in (320, 375, 390):
            self.page.set_viewport_size({'width': width, 'height': 844})
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
            for team in self.page.locator('.intimacy-members').all():
                boxes = [member.bounding_box() for member in team.locator('.intimacy-member').all()]
                self.assertEqual(len(boxes), 6)
                self.assertLess(max(box['y'] for box in boxes) - min(box['y'] for box in boxes), 1)
                self.assertTrue(all(boxes[i]['x'] + boxes[i]['width'] <= boxes[i+1]['x'] for i in range(5)))
            portrait_widths.append(self.page.locator('.intimacy-member img').first.bounding_box()['width'])
        self.assertGreater(portrait_widths[-1], portrait_widths[0])
        self.page.screenshot(path=str(REVIEW / 'intimacy-mobile.png'), full_page=True)
        self.page.locator('.intimacy-widget').screenshot(path=str(REVIEW / 'intimacy-thumbnail.png'))
        self.page.get_by_role('button', name='中止', exact=True).click()
        self.assertTrue(self.page.get_by_role('button', name='中止并归还中…').is_disabled())
        self.page.evaluate("applyIntimacy('alpha', {running:false,progress:{...DATA.progress,message:'已中止，原装备与羁绊已归还'}})")
        self.assertTrue(self.page.get_by_role('button', name='启动', exact=True).is_enabled())
        self.assertFalse(self.page.locator('.intimacy-body').is_visible())
        self.assertEqual(self.page.locator('.intimacy-member').count(), 0)
        self.page.get_by_role('button', name='启动', exact=True).click()
        self.page.get_by_role('button', name='中止', exact=True).wait_for()
        self.assertEqual(self.page.locator('.intimacy-member').count(), 6)
        self.assertNotIn('已归还', self.page.locator('.intimacy-body').inner_text())
        self.assertEqual(self.page.evaluate('calls.map(x=>x.path)'), ['/api/intimacy/start', '/api/intimacy/cancel', '/api/intimacy/start'])

    def test_account_switch_and_existing_worker_snapshot_recover_the_right_button(self):
        self.page.evaluate("applyIntimacy('alpha', DATA); activeAccount='beta'; mount();")
        self.assertTrue(self.page.get_by_role('button', name='启动', exact=True).is_visible())
        self.assertTrue(self.page.locator('.intimacy-body').evaluate("el => el.classList.contains('hidden')"))
        self.page.evaluate("activeAccount='alpha'; mount();")
        self.assertTrue(self.page.get_by_role('button', name='中止', exact=True).is_visible())
        self.assertTrue(self.page.locator('.intimacy-member[aria-label^="卡洛琳"]').is_visible())
        self.page.set_viewport_size({'width': 1280, 'height': 950})
        self.assertGreater(self.page.locator('.intimacy-member img').first.bounding_box()['width'], 74)
        self.page.screenshot(path=str(REVIEW / 'intimacy-desktop.png'), full_page=True)

    def test_missing_upper_bound_is_not_fabricated_and_names_are_escaped(self):
        self.page.evaluate("applyIntimacy('alpha', {running:true,progress:{...DATA.progress,teams:{upper:[{staticId:'H026',name:'<script>bad</script>',intimacyLevel:3,intimacyExp:25}]}}})")
        text = self.page.locator('.intimacy-body').inner_text()
        self.assertIn('25/—', text)
        self.assertNotIn('进度', text)
        self.assertNotIn('25 /', text)
        self.assertEqual(self.page.locator('.intimacy-meter').count(), 0)
        self.assertIn('<script>bad</script>', self.page.locator('.intimacy-member').get_attribute('aria-label'))
        self.assertEqual(self.page.locator('.intimacy-body script').count(), 0)

    def test_all_unfinished_on_field_heroes_include_mains_and_adapt_from_one_to_eight(self):
        self.page.evaluate("""{
          const snapshot=structuredClone(DATA);
          snapshot.progress.teams.upper[0].intimacyLevel=8;
          snapshot.progress.teams.upper[0].intimacyExp=89;
          snapshot.progress.teams.upper[0].intimacyMax=300;
          snapshot.progress.teams.lower[0].intimacyLevel=9;
          snapshot.progress.teams.lower[0].intimacyExp=200;
          snapshot.progress.teams.lower[0].intimacyMax=400;
          snapshot.progress.teams.lower[1].carrier=true;
          applyIntimacy('alpha',snapshot);
        }""")
        self.assertEqual(self.page.locator('.intimacy-member').count(),8)
        self.assertTrue(self.page.locator('.intimacy-member[aria-label="菲莉斯"]').is_visible())
        self.assertTrue(self.page.locator('.intimacy-member[aria-label="璐茜娅"]').is_visible())
        for width in (320,390,768):
            self.page.set_viewport_size({'width':width,'height':844})
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
            boxes=[row.bounding_box() for row in self.page.locator('.intimacy-member').all()]
            self.assertGreaterEqual(min(row['width'] for row in boxes),60)
            self.assertEqual(len({round(row['y']) for row in boxes}),2 if width<=480 else 1)
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('.intimacy-widget').screenshot(path=str(REVIEW/'intimacy-unfinished-eight.png'))
        self.page.evaluate("""{
          const teams=structuredClone(DATA.progress.teams);
          for(const team of Object.values(teams)) for(const hero of team) hero.intimacyLevel=10;
          teams.upper[0].intimacyLevel=9;
          teams.upper[0].intimacyMax=400;
          applyIntimacy('alpha',{running:true,progress:{...DATA.progress,teams}});
        }""")
        self.assertEqual(self.page.locator('.intimacy-member').count(),1)
        self.assertTrue(self.page.locator('.intimacy-member[aria-label="菲莉斯"]').is_visible())
        self.assertNotIn('已满',self.page.locator('.intimacy-body').inner_text())

    def test_old_recovery_has_no_manual_retry_and_start_remains_available(self):
        self.page.evaluate("applyIntimacy('alpha', {running:false,progress:{...DATA.progress,ok:false,error:true,recoveryRequired:true,message:'装备尚未完全归还：No Role'}})")
        self.assertEqual(self.page.locator('.intimacy-restore').count(),0)
        self.assertEqual(self.page.locator('.intimacy-error').count(), 0)
        self.assertEqual(self.page.locator('.intimacy-member').count(), 0)
        self.assertTrue(self.page.locator('.intimacy-body').is_visible())
        self.assertIn('点击启动会先归还上次借用的装备与羁绊', self.page.locator('.intimacy-body').inner_text())
        self.page.get_by_role('button',name='启动',exact=True).click()
        self.page.get_by_role('button',name='中止',exact=True).wait_for()
        self.assertEqual(self.page.evaluate('calls.map(row=>row.path)'),['/api/intimacy/start'])
        self.assertNotIn('No Role',self.page.locator('.intimacy-body').inner_text())

    def test_automatic_recovery_wait_remains_running_and_shows_the_reason(self):
        self.page.evaluate("applyIntimacy('alpha', {running:true,progress:{...DATA.progress,waiting:true,message:'正在核对服务器亲密度，5秒后自动重试'}})")
        self.assertIn('5秒后自动重试', self.page.locator('.intimacy-waiting').inner_text())
        self.assertEqual(self.page.locator('.intimacy-error').count(), 0)
        self.assertEqual(self.page.locator('.intimacy-member').count(), 6)
        self.assertTrue(self.page.get_by_role('button',name='中止',exact=True).is_enabled())

    def test_pending_start_is_visible_and_start_failure_is_reported_beside_button(self):
        self.page.evaluate('startPending = true;')
        self.page.get_by_role('button', name='启动', exact=True).click()
        self.assertIn('正在启动自动刷亲密度', self.page.locator('.intimacy-waiting').inner_text())
        self.assertTrue(self.page.get_by_role('button', name='处理中…', exact=True).is_disabled())
        self.page.evaluate('releaseStart();')
        self.page.get_by_role('button', name='中止', exact=True).wait_for()
        self.page.evaluate("applyIntimacy('alpha', {running:false,progress:{cancelled:true}}); startFailure = '账号连接失败';")
        self.page.get_by_role('button', name='启动', exact=True).click()
        self.assertIn('启动失败：账号连接失败', self.page.locator('.intimacy-error').inner_text())
        self.assertTrue(self.page.get_by_role('button', name='启动', exact=True).is_enabled())
        self.assertEqual(self.page.locator('.intimacy-member').count(), 0)

    def test_first_clear_status_and_terminal_failure_remain_visible(self):
        self.page.evaluate("applyIntimacy('alpha', {running:true,progress:{phase:'first_clear',message:'深渊第1关尚未通关，正在按深渊规则组队，通关后自动刷亲密度',teams:{}}})")
        self.assertIn('正在按深渊规则组队', self.page.locator('.intimacy-waiting').inner_text())
        self.assertTrue(self.page.get_by_role('button', name='中止', exact=True).is_enabled())
        self.page.evaluate("applyIntimacy('alpha', {running:true,progress:{...DATA.progress,phase:'first_clear',message:'先通关深渊第1关：正在挑战'}})")
        self.assertIn('先通关深渊第1关', self.page.locator('.intimacy-waiting').inner_text())
        self.assertEqual(self.page.locator('.intimacy-member').count(), 6)
        self.page.locator('.intimacy-widget').screenshot(path=str(REVIEW / 'intimacy-first-clear.png'))
        self.page.evaluate("applyIntimacy('alpha', {running:false,progress:{...DATA.progress,ok:false,message:'深渊第1关首通未完成：没有合适阵容 <script>bad</script>'}})")
        self.assertIn('首通未完成', self.page.locator('.intimacy-error').inner_text())
        self.assertIn('<script>bad</script>', self.page.locator('.intimacy-error').inner_text())
        self.assertEqual(self.page.locator('.intimacy-body script').count(), 0)
        self.assertEqual(self.page.locator('.intimacy-member').count(), 0)
        self.assertTrue(self.page.get_by_role('button', name='启动', exact=True).is_enabled())


if __name__ == '__main__':
    unittest.main()
