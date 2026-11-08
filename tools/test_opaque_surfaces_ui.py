"""Solid mobile surfaces and the repaired image nodes, using offline game data."""
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from backend import appcore,recruitment
from tools import test_account_switch_ui as base
from tools.test_recruitment_ui import example_data

REVIEW=Path(__file__).resolve().parents[1]/'build/opaque-assets-136-review'


class SolidSurfaces(unittest.TestCase):
    setUpClass=classmethod(base.AccountSwitchUI.setUpClass.__func__)
    tearDownClass=classmethod(base.AccountSwitchUI.tearDownClass.__func__)
    setUp=base.AccountSwitchUI.setUp
    tearDown=base.AccountSwitchUI.tearDown

    def serve(self,route):
        path=urlsplit(route.request.url).path
        if path=='/api/roster' and hasattr(self,'roster_data'):
            route.fulfill(content_type='application/json',body=json.dumps(self.roster_data));return
        if path=='/api/recruitment/refresh':
            data=example_data();definitions=recruitment.catalog()['results']
            for pool in data['pools']:
                pool['recent']=[{'sid':sid,**definitions[sid],'time':1793232000000,'lightDark':False} for sid in ('A0007','A0131')]
            route.fulfill(content_type='application/json',body=json.dumps({'recruitment':data}));return
        if path=='/api/gvg/catalog':
            route.fulfill(content_type='application/json',body=json.dumps({'characters':[],'artifacts':[],'versions':{}}));return
        base.AccountSwitchUI.serve(self,route)

    def test_all_main_modules_have_solid_painted_surfaces_in_both_modes(self):
        report=[]
        for mode in ('dark','light'):
            self.page.evaluate("mode=>setTheme({mode,accent:'#ff7fac',accent2:'#a06bff'})",mode)
            for section in ('info','auto','manual','guild','equipment','gvg','recruitment','toolbox','logs','settings','about'):
                self.page.evaluate('s=>showSection(s)',section);self.page.wait_for_timeout(60)
                if section=='recruitment':self.page.wait_for_function("document.querySelector('.ra-pool') && !document.querySelector('[data-refresh]').disabled")
                painted=self.page.evaluate("""() => {
                  const bad=[],surfaces=[];
                  for(const el of document.querySelectorAll('body *')) {
                    if(el instanceof SVGElement)continue;
                    const rect=el.getBoundingClientRect(),s=getComputedStyle(el);
                    if(!rect.width||!rect.height||s.visibility==='hidden'||s.display==='none')continue;
                    const color=s.backgroundColor,match=color.match(/rgba\\([^,]+,[^,]+,[^,]+,\\s*([\\d.]+)\\)/);
                    if(match && Number(match[1])>0 && Number(match[1])<1)bad.push([el.id||el.className,color]);
                    if(el.matches('.topbar,.menu-btn,.pill,.ra-tabs,.ra-empty,.card,.bottom-log-dock'))surfaces.push([el.className,color]);
                  }
                  return {bad,surfaces};
                }""")
                self.assertEqual(painted['bad'],[],(mode,section,painted['bad']))
                self.assertTrue(all(not color.startswith('rgba') for _,color in painted['surfaces']),(mode,section,painted['surfaces']))
                report.append({'mode':mode,'section':section,'solidSurfaces':len(painted['surfaces'])})
            self.page.screenshot(path=str(REVIEW/f'opaque-{mode}-settings.png'))
        (REVIEW/'surface-audit.json').write_text(json.dumps(report,indent=2))

    def test_restored_portraits_and_currency_labels_preserve_nodes_on_inventory_poll(self):
        state={'RoleDataContainer':{'Roles':[{'StaticID':sid,'LV':1} for sid in ('M0023','M0191','M0192','M0193')]},
               'ItemContainer':{'Items':[{'StaticID':str(i),'Count':i} for i in range(40,64)]+[
                   {'StaticID':sid,'Count':88} for sid in ('AH002','AH175','AH188','AH120','AH806')]}}
        roster=appcore._roster(state)
        self.roster_data=roster
        self.page.evaluate("activeAcc().roster=null;showSection('info')")
        self.page.wait_for_function("document.querySelectorAll('#rosterHeroes .hero-card').length===4")
        self.page.wait_for_function("[...document.querySelectorAll('#rosterHeroes img')].every(i=>i.complete&&i.naturalWidth)")
        self.assertEqual(self.page.locator('#rosterHeroes .hero-ph').count(),0)
        self.page.locator('[data-rtab=items]').click()
        self.assertEqual(self.page.locator('#rosterItems .item-tile').count(),5)
        self.assertEqual(self.page.locator('#rosterItems [data-item-id="40"]').count(),0)
        self.page.locator('[data-item-category=other]').click()
        self.page.locator('#rosterItems img').evaluate_all("imgs=>imgs.forEach(i=>i.loading='eager')")
        self.page.wait_for_function("[...document.querySelectorAll('#rosterItems img')].every(i=>i.complete&&i.naturalWidth)")
        self.assertEqual(self.page.locator('.item-label').count(),24)
        self.assertIn('第1章',self.page.locator('[data-item-id="40"]').inner_text())
        self.assertIn('贡献·54',self.page.locator('[data-item-id="54"]').inner_text())
        self.page.evaluate("window.iconBefore=document.querySelector('[data-item-id=\\\"40\\\"] img')")
        roster['items'][0]['count']+=1
        self.page.evaluate('items=>updateRosterItems(items)',roster['items'])
        self.assertTrue(self.page.evaluate("iconBefore===document.querySelector('[data-item-id=\\\"40\\\"] img')"))
        self.page.screenshot(path=str(REVIEW/'inventory-labels-mobile.png'))
        self.page.evaluate("showSection('recruitment')")
        self.page.wait_for_function("document.querySelector('.ra-pool')")
        self.page.locator('.ra-result img').evaluate_all("imgs=>imgs.forEach(i=>i.loading='eager')")
        self.page.wait_for_function("[...document.querySelectorAll('.ra-result img')].every(i=>i.complete&&i.naturalWidth)")
        self.assertEqual(self.page.locator('.ra-result img').count(),8)
        self.page.locator('[data-pool=NormalSummon] .ra-results').screenshot(path=str(REVIEW/'repaired-recruitment-icons.png'))


if __name__=='__main__':unittest.main()
