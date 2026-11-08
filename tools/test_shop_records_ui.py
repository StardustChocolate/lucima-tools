"""Monthly/daily shop folds and compact real-ledger layouts on phone widths."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from backend import config,recruitment as archive
from tools import test_recruitment_ui as base
from tools.test_recruitment import STAMP,shelves
from tools.test_shop_records import shelf,purchase

REVIEW=Path(__file__).resolve().parents[1]/'build/shop-records-137-review'


def detailed_data():
    result=base.example_data()
    with TemporaryDirectory() as directory,patch.object(config,'_DATA_DIR',Path(directory)):
        client=SimpleNamespace(aid='offline-ui',account_state={})
        for stamp,paid in ((STAMP,1),(STAMP-86400000,0),(STAMP-30*86400000,1)):
            response=shelves(stamp);response['CostItems']=[{'Item':{'StaticID':'2','Count':30}}] if paid else []
            archive.observe_shop(client,{'StoreID':'SecretShop','IsUseGold':paid},response)
        records=[shelf(sid,slot=sid) for sid in ('5','6','EC11','EC21','EC31','EC41','EC51','EC61')]
        records[0]['DropResult']['Items'][0]['Item']['Count']=5
        records[1]['DropResult']['Items'][0]['Item']['Count']=50
        records+=[shelf(slot='equip-'+sid,equip={'StaticID':sid,'Set':set_id})
                  for sid,set_id in (('E0011','Speed'),('E0021','Speed'),('E0012','Attack'))]
        for r in records:archive.observe_shop_purchase(client,{'Record':r,'Count':1},purchase(r,gold=100000))
        result['shop']=archive.shop_summary(archive.account_key(client),archive.catalog())
    return result


class ShopRecordsUI(unittest.TestCase):
    setUpClass=classmethod(base.RecruitmentUI.setUpClass.__func__)
    tearDownClass=classmethod(base.RecruitmentUI.tearDownClass.__func__)
    tearDown=base.RecruitmentUI.tearDown
    serve=base.RecruitmentUI.serve
    finish=base.RecruitmentUI.finish
    enter=base.RecruitmentUI.enter

    def setUp(self):
        base.RecruitmentUI.setUp(self)
        self.responses['first']=detailed_data()
        self.enter();self.page.locator('[data-tab=shop]').click()

    def test_month_button_folds_the_whole_month_and_daily_button_is_independent(self):
        head=self.page.locator('[data-shop-month="2026-10"]')
        self.assertEqual(head.get_attribute('aria-expanded'),'true')
        self.assertEqual(head.locator('use').get_attribute('href'),'#ui-chevron-up')
        self.assertEqual(self.page.locator('[data-shop-month="2026-09"]').get_attribute('aria-expanded'),'false')
        self.assertEqual(self.page.locator('.ra-shop-day').count(),0)
        day_button=self.page.locator('[data-shop-days="2026-10"]')
        day_button.click()
        self.assertEqual(self.page.locator('.ra-shop-day').count(),2)
        self.assertEqual(day_button.get_attribute('aria-expanded'),'true')
        self.assertEqual(day_button.locator('use').get_attribute('href'),'#ui-chevron-up')
        head.click()
        self.assertEqual(self.page.locator('[data-month="2026-10"] .ra-month-body').count(),0)
        self.assertEqual(head.locator('use').get_attribute('href'),'#ui-chevron-down')
        head.click()
        self.assertEqual(self.page.locator('.ra-shop-day').count(),2)
        day_button.click()
        self.assertEqual(self.page.locator('.ra-shop-day').count(),0)
        self.assertEqual(day_button.locator('use').get_attribute('href'),'#ui-chevron-down')
        self.page.locator('[data-shop-month="2026-09"]').click()
        self.assertEqual(self.page.locator('.ra-month-body').count(),2)

    def test_purchased_counts_parts_sets_spending_and_day_totals_are_visible(self):
        overview=self.page.locator('#ra-month-2026-10 > .ra-shop-overview')
        self.assertIn('5 张',overview.locator('[data-ticket=recruit]').inner_text())
        self.assertIn('50 张',overview.locator('[data-ticket=mystery]').inner_text())
        self.assertEqual(overview.locator('.ra-chip').count(),6)
        self.assertEqual(overview.locator('.ra-equipment-row').count(),2)
        self.assertIn('武器',overview.locator('.ra-equipment-row').first.inner_text())
        self.assertIn('速度套装',overview.locator('.ra-equipment-row').first.inner_text())
        self.assertIn('2 件',overview.locator('.ra-equipment-row').first.inner_text())
        self.assertIn('1,100,000',overview.locator('.ra-shop-spending').inner_text())
        self.assertIn('30',overview.locator('.ra-shop-spending').inner_text())
        self.page.locator('[data-shop-days="2026-10"]').click()
        day=self.page.locator('[data-day="2026-10-29"]')
        self.assertEqual(day.locator('.ra-day-gains').count(),1)
        self.assertEqual(day.locator('.ra-day-costs').count(),1)
        self.assertEqual(day.locator('.ra-chip,.ra-equipment-row,.ra-ticket,.ra-shop-spending').count(),0)
        self.assertIn('招募契约：5',day.locator('.ra-day-gains').inner_text())
        self.assertIn('神秘契约：50',day.locator('.ra-day-gains').inner_text())
        self.assertIn('速度·武器：2',day.locator('.ra-day-gains').inner_text())
        self.assertIn('1,100,000',day.locator('.ra-day-costs').inner_text())

    def test_refresh_keeps_fold_choices_and_account_switch_does_not_reuse_them(self):
        self.page.locator('[data-shop-days="2026-10"]').click()
        self.page.locator('[data-shop-month="2026-09"]').click()
        self.page.locator('[data-refresh]').click()
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.assertEqual(self.page.locator('.ra-month-body').count(),2)
        self.assertEqual(self.page.locator('.ra-shop-day').count(),2)
        self.page.evaluate("activeAccount='second';window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.assertEqual(self.page.locator('.ra-month').count(),0)
        self.page.evaluate("activeAccount='first';window.dispatchEvent(new CustomEvent('lucima:account-change'))")
        self.page.wait_for_function("!document.querySelector('[data-refresh]').disabled")
        self.assertEqual(self.page.locator('.ra-month-body').count(),2)
        self.assertEqual(self.page.locator('.ra-shop-day').count(),2)

    def test_phone_and_desktop_have_two_ticket_columns_and_no_overflow_in_both_themes(self):
        self.page.locator('[data-shop-days="2026-10"]').click()
        for width in (320,390,820,1440):
            self.page.set_viewport_size({'width':width,'height':900})
            for mode in ('dark','light'):
                self.page.evaluate("mode=>{setTheme({mode});setDrawer(false)}",mode)
                self.page.wait_for_timeout(60)
                self.page.locator('#recruitmentRoot img').evaluate_all("imgs=>imgs.forEach(i=>i.loading='eager')")
                self.page.wait_for_function("[...document.querySelectorAll('#recruitmentRoot img')].every(i=>i.complete&&i.naturalWidth)")
                tickets=self.page.locator('#ra-month-2026-10 > .ra-shop-overview > .ra-tickets .ra-ticket')
                a,b=[tickets.nth(i).bounding_box() for i in range(2)]
                self.assertAlmostEqual(a['y'],b['y'],delta=1)
                self.assertLess(a['x'],b['x'])
                overflow=self.page.evaluate("""() => [...document.querySelectorAll('#recruitmentRoot *')].filter(n=>{
                  const b=n.getBoundingClientRect(),r=document.getElementById('recruitmentRoot').getBoundingClientRect();
                  return b.width>0&&(b.right>r.right+2||b.left<r.left-2||n.scrollWidth>n.clientWidth+2);
                }).map(n=>n.className)""")
                self.assertEqual(overflow,[],(width,mode,overflow))
                if width==390:
                    day=self.page.locator('[data-day="2026-10-29"]')
                    self.assertLess(day.bounding_box()['height'],190)
                    day.screenshot(path=str(REVIEW/f'daily-compact-{mode}.png'))
                self.page.locator('[data-month="2026-10"]').screenshot(path=str(REVIEW/f'shop-{mode}-{width}.png'))


if __name__=='__main__':unittest.main()
