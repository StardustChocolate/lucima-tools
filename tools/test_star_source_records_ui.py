"""Three record tabs and monthly-only star-source equipment on narrow screens."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from backend import config, recruitment as archive
from tools import test_recruitment_ui as base
from tools.test_shop_records_ui import detailed_data
from tools.test_star_source_records import star_record, star_receipt

REVIEW = Path(__file__).resolve().parents[1] / 'build/star-source-records-review'


def star_data():
    result = detailed_data()
    with TemporaryDirectory() as directory, patch.object(config, '_DATA_DIR', Path(directory)):
        client = SimpleNamespace(aid='star-ui-account', account_state={})
        for index, (sid, set_id, stamp) in enumerate((('E0011', 'Speed', 1793270587150),
                                                     ('E0021', 'Speed', 1793270587150),
                                                     ('E0012', 'Attack', 1793270587150),
                                                     ('E0011', 'Health', 1790000000000))):
            archive.observe_shop_purchase(client, {'Record': star_record(), 'ItemIndex': 0, 'Count': 1},
                                          star_receipt(str(index), sid, set_id, stamp=stamp))
        result['shop']['starSource'] = archive.shop_summary(archive.account_key(client), archive.catalog())['starSource']
    return result


class StarSourceUI(unittest.TestCase):
    setUpClass = classmethod(base.RecruitmentUI.setUpClass.__func__)
    tearDownClass = classmethod(base.RecruitmentUI.tearDownClass.__func__)
    tearDown = base.RecruitmentUI.tearDown
    serve = base.RecruitmentUI.serve
    finish = base.RecruitmentUI.finish
    enter = base.RecruitmentUI.enter

    def setUp(self):
        base.RecruitmentUI.setUp(self)
        self.responses['first'] = star_data()
        self.enter()

    def test_names_tab_order_and_monthly_equipment_only(self):
        self.assertEqual(self.page.locator('.ra-tabs button').all_text_contents(),
                         ['抽卡', '秘密商店', '星源商店'])
        for selector in ('#crumb', '#sideNav [data-section=recruitment]', '.ra-heading h2'):
            self.assertIn('出货记录', self.page.locator(selector).inner_text())
        self.page.locator('[data-tab=star]').click()
        self.assertEqual(self.page.locator('.ra-star-month').count(), 2)
        month = self.page.locator('[data-star-month="2026-10"]')
        self.assertIn('已购 3 件', month.inner_text())
        rows = month.locator('.ra-equipment-row')
        self.assertEqual(rows.count(), 2)
        self.assertIn('武器', rows.first.inner_text())
        self.assertIn('速度套装', rows.first.inner_text())
        self.assertIn('2 件', rows.first.inner_text())
        self.assertIn('头盔', rows.nth(1).inner_text())
        self.assertIn('攻击套装', rows.nth(1).inner_text())
        self.assertEqual(self.page.locator('.ra-days-toggle,.ra-shop-day,.ra-chips,.ra-shop-spending,.ra-ticket').count(), 0)

    def test_star_folds_remain_independent_of_secret_and_keep_direction_after_refresh(self):
        self.page.locator('[data-tab=shop]').click()
        self.page.locator('[data-shop-month="2026-10"]').click()
        self.page.locator('[data-tab=star]').click()
        head = self.page.locator('[data-star-toggle="2026-10"]')
        self.assertEqual(head.get_attribute('aria-expanded'), 'true')
        self.assertEqual(head.locator('use').get_attribute('href'), '#ui-chevron-up')
        head.click()
        self.assertEqual(head.get_attribute('aria-expanded'), 'false')
        self.assertEqual(head.locator('use').get_attribute('href'), '#ui-chevron-down')
        self.page.locator('[data-star-toggle="2026-09"]').click()
        self.page.locator('[data-refresh]').click()
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.assertEqual(head.get_attribute('aria-expanded'), 'false')
        self.assertTrue(self.page.locator('#ra-star-month-2026-09').is_visible())
        self.page.locator('[data-tab=shop]').click()
        self.assertEqual(self.page.locator('[data-shop-month="2026-10"]').get_attribute('aria-expanded'), 'false')

    def test_newly_recorded_purchase_updates_on_reentry_without_other_account_leak(self):
        self.page.locator('[data-tab=star]').click()
        star = self.responses['first']['shop']['starSource']['months'][0]
        star['equipment'][0]['count'] += 1; star['total'] += 1
        self.page.evaluate("showSection('info');showSection('recruitment')")
        self.page.wait_for_function("document.querySelector('.ra-star-month')?.textContent.includes('已购 4 件') && !document.querySelector('[data-refresh]').disabled")
        self.page.evaluate("activeAccount='second';window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.page.locator('[data-tab=star]').click()
        self.assertEqual(self.page.locator('.ra-star-month').count(), 0)
        self.assertIn('还没有星源商店购买记录', self.page.locator('#recruitmentRoot').inner_text())

    def test_switching_long_history_to_empty_shops_keeps_toolbar_and_paints_one_bounded_view(self):
        self.responses['first']['shop']['months'] = []
        self.responses['first']['shop']['starSource']['months'] = []
        self.page.locator('[data-refresh]').click()
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.page.evaluate("window.recordToolbar=document.querySelector('.ra-toolbar');window.recordHeading=document.querySelector('.ra-heading')")
        for tab in ('shop', 'summon', 'star', 'summon', 'shop'):
            self.page.evaluate("document.querySelector('.content').scrollTop=1000")
            self.page.locator(f'[data-tab={tab}]').evaluate('n=>n.click()')
            self.assertTrue(self.page.evaluate("recordToolbar===document.querySelector('.ra-toolbar')&&recordHeading===document.querySelector('.ra-heading')"))
            self.assertEqual(self.page.locator('.ra-view').count(), 1)
            self.assertEqual(self.page.evaluate("document.querySelector('.content').scrollTop"), 0)
            if tab != 'summon':
                bounds = self.page.locator('.ra-empty').evaluate("n=>{const r=n.getBoundingClientRect(),c=n.lastElementChild.getBoundingClientRect(),s=getComputedStyle(n);return {height:r.height,inside:c.bottom<=r.bottom,bg:s.backgroundColor,contain:s.contain}}")
                self.assertGreaterEqual(bounds['height'], 220)
                self.assertTrue(bounds['inside'])
                self.assertFalse(bounds['bg'].startswith('rgba'))
                self.assertIn('paint', bounds['contain'])
                self.assertEqual(self.page.locator('.ra-pool,.ra-month').count(), 0)

    def test_empty_shop_does_not_claim_no_records_while_refresh_is_pending(self):
        self.responses['first']['shop']['starSource']['months'] = []
        self.page.locator('[data-refresh]').click()
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.page.locator('[data-tab=star]').click()
        self.hold = True
        self.page.locator('[data-refresh]').click()
        self.page.wait_for_function("document.querySelector('.ra-empty').textContent.includes('正在同步星源商店记录')")
        self.assertNotIn('还没有', self.page.locator('.ra-empty').inner_text())
        self.hold = False
        for route, account in self.held: self.finish(route, account)
        self.held.clear()
        self.page.wait_for_function("document.querySelector('.ra-empty').textContent.includes('还没有星源商店购买记录')")

    def test_all_three_tabs_fit_mobile_and_desktop_with_opaque_surfaces(self):
        REVIEW.mkdir(parents=True, exist_ok=True)
        for width in (320, 390, 600, 960, 1440):
            self.page.set_viewport_size({'width': width, 'height': 900})
            for mode in ('dark', 'light'):
                self.page.evaluate('mode=>{setTheme({mode});setDrawer(false)}', mode)
                for tab in ('summon', 'shop', 'star'):
                    self.page.locator(f'[data-tab={tab}]').click()
                    self.page.wait_for_timeout(50)
                    overflow = self.page.evaluate("""() => [...document.querySelectorAll('#recruitmentRoot *')].filter(n=>{
                      const b=n.getBoundingClientRect(),r=document.getElementById('recruitmentRoot').getBoundingClientRect();
                      return b.width>0&&(b.right>r.right+2||b.left<r.left-2||n.scrollWidth>n.clientWidth+2);
                    }).map(n=>n.className)""")
                    self.assertEqual(overflow, [], (width, mode, tab, overflow))
                self.assertEqual(self.page.locator('.ra-equipment-row').evaluate_all("nodes=>nodes.filter(n=>getComputedStyle(n).backgroundColor.includes('rgba')).length"), 0)
                self.page.screenshot(path=str(REVIEW / f'star-{mode}-{width}.png'))


if __name__ == '__main__': unittest.main()
