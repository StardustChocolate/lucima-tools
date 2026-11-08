"""Audit actual UI colors across every main page and expanded shop states."""
import json
from pathlib import Path
import unittest

from tools import test_opaque_surfaces_ui as base

REVIEW = Path(__file__).resolve().parents[1] / 'build/all-solid-surfaces-review'
AUDIT = """() => {
  const issues=[]; let painted=0;
  const partial=value=>[...value.matchAll(/rgba\\([^)]*,\\s*([\\d.]+)\\)/g)].some(m=>Number(m[1])>0&&Number(m[1])<1);
  for(const n of document.querySelectorAll('body *')) {
    if(n instanceof SVGElement)continue;
    const b=n.getBoundingClientRect(),s=getComputedStyle(n);
    if(!b.width||!b.height||s.visibility==='hidden'||s.display==='none'||Number(s.opacity)===0)continue;
    const name=n.id||n.className||n.tagName;
    if(Number(s.opacity)>0&&Number(s.opacity)<1)issues.push([name,'opacity',s.opacity]);
    for(const key of ['backgroundColor','backgroundImage','color','outlineColor'])if(partial(s[key]))issues.push([name,key,s[key]]);
    for(const side of ['Top','Right','Bottom','Left'])if(parseFloat(s['border'+side+'Width'])>0&&s['border'+side+'Style']!=='none'&&partial(s['border'+side+'Color']))issues.push([name,'border'+side,s['border'+side+'Color']]);
    if(n.matches('.card,.auto-item,.shop-widget,.shop-head,.shop-body,.card-area,.card-empty,.ra-view,.ra-empty,.ra-month,.topbar,.pill,.menu-btn,.bottom-log-dock')) {
      painted++;
      if(s.backgroundColor==='rgba(0, 0, 0, 0)')issues.push([name,'missingFill',s.backgroundColor]);
    }
  }
  return {issues,painted};
}"""


class AllSurfaceColors(unittest.TestCase):
    setUpClass = classmethod(base.SolidSurfaces.setUpClass.__func__)
    tearDownClass = classmethod(base.SolidSurfaces.tearDownClass.__func__)
    setUp = base.SolidSurfaces.setUp
    tearDown = base.SolidSurfaces.tearDown
    serve = base.SolidSurfaces.serve

    def audit(self, label):
        result = self.page.evaluate(AUDIT)
        self.assertEqual(result['issues'], [], (label, result['issues']))
        return {'view': label, 'painted': result['painted']}

    def test_all_main_pages_both_themes_and_expanded_shop_error_empty_states(self):
        REVIEW.mkdir(parents=True, exist_ok=True)
        report=[]
        for mode in ('dark','light'):
            self.page.evaluate('mode=>setTheme({mode})', mode)
            for section in ('info','auto','manual','guild','equipment','gvg','recruitment','toolbox','logs','settings','about'):
                self.page.evaluate('s=>showSection(s)', section)
                self.page.wait_for_timeout(100)
                report.append(self.audit(mode+'-'+section))
                if section == 'manual':
                    self.page.locator('#shop-shop_diamond .shop-toggle').click()
                    self.page.evaluate("renderSecretShopState('shop_diamond',{kind:'error',error:'CantReset'})")
                    self.page.locator('#starshop-star_source_shop .shop-toggle').click()
                    self.page.wait_for_timeout(100)
                    report.append(self.audit(mode+'-expanded-shops'))
                    self.page.locator('#shop-shop_diamond').screenshot(path=str(REVIEW/f'shop-error-{mode}.png'))
                if section == 'recruitment':
                    self.page.wait_for_function("document.querySelector('[data-refresh]')&&!document.querySelector('[data-refresh]').disabled")
                    for tab in ('summon','shop','star'):
                        self.page.locator(f'[data-tab={tab}]').click()
                        report.append(self.audit(mode+'-records-'+tab))
                self.page.screenshot(path=str(REVIEW/f'{section}-{mode}.png'))
        (REVIEW/'all-pages-colors.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))

    def test_toolbox_subpages_popovers_and_inventory_borders_are_solid(self):
        from backend import appcore
        self.roster_data = appcore._roster({'ItemContainer': {'Items': [
            {'StaticID': sid, 'Count': 88} for sid in ('AH002','AH175','AH188','AH120','AH806')]}})
        for i, item in enumerate(self.roster_data['items']): item['rank'] = i+1
        for mode in ('dark','light'):
            self.page.evaluate('mode=>setTheme({mode,accent:"#445566",accent2:"#bd6348"})', mode)
            self.page.evaluate("activeAcc().roster=null;showSection('info')")
            self.page.wait_for_function("document.querySelectorAll('.item-tile').length===5")
            self.page.locator('[data-rtab=items]').click()
            self.audit(mode+'-inventory-rarities')
            for tool in ('equipment-filter','encyclopedia','background-limit-test'):
                self.page.evaluate('tool=>{showSection("toolbox");showToolboxPage(tool)}', tool)
                self.page.wait_for_timeout(80)
                self.audit(mode+'-'+tool)
            self.page.evaluate("void openConfirmDialog({title:'颜色检查',message:'检查弹层与按钮颜色',tone:'warning'})")
            self.audit(mode+'-confirmation')
            self.page.locator('.confirm-cancel').click()


if __name__ == '__main__': unittest.main()
