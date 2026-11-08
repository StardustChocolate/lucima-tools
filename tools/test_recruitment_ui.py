"""Full application UI: fresh entry, account races, lifecycle and narrow layouts."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from backend import recruitment as archive
from tools import test_guild_ui as base

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'build/recruitment-review'


def example_data(total=169, shop=True):
    data = archive.catalog()
    pools = []
    for sid in ('NormalSummon', 'MysticSummonH133', 'AcyivitySummonH178', 'GalaxySummon'):
        definition = data['pools'][sid]
        definitions = data['results']
        ids = ['H001', 'H178', 'A0350', 'H133', 'A0350', 'H133']
        low = next(sid for sid, row in definitions.items() if row['star'] == 3 and row['kind'] == 'role')
        logs = [{'id': str(i), 'sid': ids[(i // 32) % len(ids)] if i % 32 == 0 else low, 'time': 1793200000000 + i * 1000} for i in range(total)]
        pool = archive.statistics(definition, logs, {'GuaranteedCount': 131 if sid == 'NormalSummon' else 31}, data,
                                  {'updatedAt': 1793232000000, 'firstSyncAt': 1793100000000})
        pool.update(active=True, error=None)
        pools.append(pool)
    return {'updatedAt': 1793232000000, 'attemptedAt': 1793232000000, 'pools': pools, 'errors': [],
            'shop': {'coverage': '仅累计本工具成功刷新的货架，含免费与钻石刷新；启用前和其他客户端刷新不计入。',
                     'firstRecordAt': 1793232000000, 'baselines': data['shop'],
                     'months': [{'month': '2026-10', 'refreshes': 14, 'paid': 10, 'free': 4, 'recruit': 5,
                                 'mystery': 50, 'recruitHits': 1, 'mysteryHits': 1,
                                 'days': [{'day': '2026-10-29', 'refreshes': 14, 'paid': 10, 'free': 4,
                                           'recruit': 5, 'mystery': 50, 'recruitHits': 1, 'mysteryHits': 1}]}] if shop else []}}


class RecruitmentUI(unittest.TestCase):
    setUpClass = classmethod(base.GuildUITests.setUpClass.__func__)
    tearDownClass = classmethod(base.GuildUITests.tearDownClass.__func__)
    tearDown = base.GuildUITests.tearDown

    def setUp(self):
        self.responses = {'first': example_data(), 'second': example_data(2, shop=False)}
        self.archive_calls = []
        self.hold = False
        self.held = []
        self.failed = False
        base.GuildUITests.setUp(self)

    def serve(self, route):
        if urlsplit(route.request.url).path == '/api/recruitment/refresh':
            account = route.request.post_data_json['account']
            self.archive_calls.append(account)
            if self.hold:
                self.held.append((route, account))
            else:
                self.finish(route, account)
            return
        base.GuildUITests.serve(self, route)

    def finish(self, route, account):
        route.fulfill(status=500 if self.failed else 200, content_type='application/json', body=json.dumps(
            {'detail': '网络暂时不可用'} if self.failed else {'recruitment': self.responses[account]}))

    def enter(self):
        self.page.evaluate("showSection('recruitment')")
        self.page.wait_for_function("document.querySelector('.ra-pool') && !document.querySelector('[data-refresh]').disabled")

    def test_entry_reentry_manual_refresh_and_pool_expansion(self):
        self.enter()
        self.assertEqual(self.archive_calls, ['first'])
        self.assertIn('出货记录', self.page.locator('#crumb').inner_text())
        self.assertEqual(self.page.locator('#sideNav [data-section=equipment] + button').inner_text(), '出货记录')
        self.assertIn('差 49 抽', self.page.locator('[data-pool=NormalSummon]').inner_text())
        self.page.locator('[data-collapse=NormalSummon]').click()
        self.assertEqual(self.page.locator('[data-pool=NormalSummon] .ra-pool-body').count(), 0)
        self.page.locator('[data-collapse=NormalSummon]').click()
        self.page.locator('[data-refresh]').click()
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.assertEqual(len(self.archive_calls), 2)
        self.page.evaluate("showSection('info'); showSection('recruitment')")
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.assertEqual(len(self.archive_calls), 3)
        self.page.locator('[data-filter=mystic]').click()
        self.assertEqual(self.page.locator('.ra-pool').count(), 1)
        self.assertIn('光暗五星间隔', self.page.locator('.ra-pool').inner_text())

    def test_switch_account_while_request_is_pending_never_shows_old_data(self):
        self.hold = True
        self.page.evaluate("showSection('recruitment')")
        self.page.wait_for_timeout(50)
        self.page.evaluate("activeAccount='second'; window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.page.wait_for_timeout(50)
        self.assertEqual(self.archive_calls, ['first', 'second'])
        self.assertNotIn('169 抽', self.page.locator('#recruitmentRoot').inner_text())
        for route, account in self.held: self.finish(route, account)
        self.page.wait_for_function("document.querySelector('.ra-pool') && !document.querySelector('[data-refresh]').disabled")
        self.assertIn('2 抽', self.page.locator('[data-pool=NormalSummon]').inner_text())
        self.assertNotIn('169 抽', self.page.locator('[data-pool=NormalSummon]').inner_text())

    def test_entry_during_previous_request_requests_fresh_data_after_completion(self):
        self.hold = True
        self.page.evaluate("showSection('recruitment')")
        self.page.wait_for_timeout(50)
        self.page.evaluate("showSection('info');showSection('recruitment')")
        self.page.wait_for_timeout(50)
        self.assertEqual(len(self.archive_calls), 1)
        self.hold = False
        self.finish(*self.held.pop())
        self.page.wait_for_function("document.querySelector('.ra-pool') && !document.querySelector('[data-refresh]').disabled")
        self.assertEqual(len(self.archive_calls), 2)

    def test_sync_failure_and_empty_shop_are_explicit(self):
        self.enter(); self.failed = True
        self.page.locator('[data-refresh]').click()
        self.page.wait_for_function("document.querySelector('.ra-sync').textContent.includes('同步失败')")
        self.assertIn('上次保存数据', self.page.locator('.ra-sync').inner_text())
        self.assertIn('169 抽', self.page.locator('[data-pool=NormalSummon]').inner_text())
        self.failed = False
        self.page.evaluate("activeAccount='second';window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.page.locator('[data-tab=shop]').click()
        self.assertIn('还没有商店刷新记录', self.page.locator('#recruitmentRoot').inner_text())

    def test_desktop_and_mobile_both_themes_have_no_overflow_or_broken_images(self):
        self.enter(); ARTIFACTS.mkdir(exist_ok=True, parents=True)
        for width, height in ((1440, 1000), (390, 844), (320, 740)):
            for theme in ('dark','light'):
                self.page.set_viewport_size({'width':width,'height':height})
                self.page.evaluate("mode=>{document.documentElement.dataset.theme=mode;setDrawer(false)}", theme)
                for tab in ('summon','shop'):
                    self.page.locator(f'[data-tab={tab}]').click()
                    self.page.wait_for_timeout(80)
                    overflow = self.page.evaluate("""()=>[...document.querySelectorAll('#recruitmentRoot *')].filter(el=>{
                      const r=el.getBoundingClientRect(),root=document.getElementById('recruitmentRoot').getBoundingClientRect();
                      return r.width>0 && (r.right>root.right+2||r.left<root.left-2)
                    }).map(el=>el.className)""")
                    self.assertEqual(overflow, [], (width,theme,tab,overflow))
                    self.assertEqual(self.page.locator('#recruitmentRoot img').evaluate_all('(imgs)=>imgs.filter(i=>i.complete&&!i.naturalWidth).length'),0)
                    self.page.screenshot(path=str(ARTIFACTS/f'{tab}-{theme}-{width}.png'))
        self.page.locator('.ra-month-head').click()
        self.assertEqual(self.page.locator('.ra-shop-day').count(), 0)


if __name__ == '__main__': unittest.main()
