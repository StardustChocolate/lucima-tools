"""GVG 离线验收：真实静态技能 + 虚拟账号；禁止游戏调用。"""
import copy
import itertools
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.game_client import GameError
from backend.gvg import adapters, api, search
from backend.gvg.catalog import catalog, mechanics, versions
from equipment import plans, stats
from equipment.tests.test_auto import fixture
from equipment.tests.test_bonds import artifact


HEROES = ['H182','H121','H001','H191','H192','H197','H200','H204','H130','H189','H153','H193']


def account_fixture(count=10):
    state, roles=fixture(1,roles_count=count)
    base=state['EquipmentContainer']['Datas'];state['EquipmentContainer']['Datas']=[]
    for i,(rid,role) in enumerate(roles.items()):
        role['StaticID']=HEROES[i%len(HEROES)]
        role['Skills']={'Skills':[{'StaticID':role['StaticID']+'S1','Level':2}]}
        for raw in base:
            item=copy.deepcopy(raw);item['_id']={'$oid':rid+'-'+plans._oid(raw['_id'])};item['EquipRole']=rid
            if plans.equip_slot(item)=='boots':item['MainProp']['Value']=35+i*4
            state['EquipmentContainer']['Datas'].append(item)
    client=SimpleNamespace(account_state=state,call=Mock(side_effect=AssertionError('禁止游戏请求')),bootstrap=Mock(side_effect=AssertionError('禁止同步')))
    return SimpleNamespace(client=client,lock=threading.Lock()),roles


def request():
    return {'target':'测试对手','enemies':{half:[{'staticID':sid} for sid in ['H182','H191','H200']] for half in adapters.HALVES},'options':{}}


def wait(acc,job):
    deadline=time.monotonic()+8
    while True:
        value=api.handle(acc,'status',{'jobId':job['jobId']})
        if value['status'] not in ('running','cancelling','verifying'):return value
        if time.monotonic()>deadline:raise AssertionError('GVG 任务未结束')
        time.sleep(.01)


class InputTests(unittest.TestCase):
    def test_speed_margin_warns_until_more_than_ten_speed_ahead(self):
        acc,_=account_fixture();team=adapters.roster(acc.client.account_state)['roles'][:3]
        enemies=adapters.normalize(request())['enemies']['upper']
        for enemy in enemies:enemy['speed']={'min':250,'max':250}
        for speed,status in [(249,'fail'),(250,'fail'),(251,'unknown'),(260,'unknown'),(261,'pass')]:
            for role in team:role['current']['panel']['Speed']=speed
            result=search.assess(team,enemies,'manual')
            check=next(c for c in result['checks'] if c['id']=='panel-speed')
            self.assertEqual(check['status'],status)
            self.assertIn('10 速',check['reason'])

    def test_matchup_order_prefers_fewer_confirmed_risks_before_raw_output(self):
        safe=((1,1,1,1,1,1,1,1),None,None,None,{'checks':[{'status':'pass','required':False}]})
        risky=((1,1,1,1,1,999,999,999),None,None,None,{'checks':[{'status':'fail','required':False}]})
        self.assertGreater(search._matchup_key(safe),search._matchup_key(risky))

    def test_input_observations_keep_unknown_lower_and_current_health_distinct(self):
        value=request();value['enemies']['upper'][0].update(speed={'kind':'lower','min':280},health={'kind':'exact','context':'current','value':10000})
        result=adapters.normalize(value)['enemies']['upper'][0]
        self.assertEqual(result['speed']['min'],280);self.assertIsNone(result['speed']['max'])
        self.assertEqual(result['health']['context'],'current')
        normalized=adapters.normalize(value)
        self.assertEqual(adapters.normalize(normalized),normalized,'收藏后的规范化情报应能直接再次生成')
        self.assertEqual(adapters.observation(None,'速度')['kind'],'unknown')

    def test_invalid_values_ranges_duplicates_and_incompatible_artifacts(self):
        for bad in ('NaN','Infinity',float('nan'),-1,True,{},None):
            with self.subTest(bad=bad),self.assertRaises(GameError):adapters.observation({'kind':'exact','value':bad},'速度')
        for value in ({'kind':'interval','min':290,'max':275},{'kind':'lower','min':''},{'kind':'range'}):
            with self.assertRaises(GameError):adapters.observation(value,'速度')
        value=request();value['enemies']['upper'][1]['staticID']='H182'
        with self.assertRaises(GameError):adapters.normalize(value)
        value=request();value['enemies']['upper'][0]['artifact']='A0347'
        with self.assertRaisesRegex(GameError,'职业'):adapters.normalize(value)
        value=request();value['options']={'locks':{'upper':['r0'],'lower':['r0']}}
        with self.assertRaises(GameError):adapters.normalize(value)


class CatalogAndSnapshotTests(unittest.TestCase):
    def test_actual_skills_and_mechanisms_have_structured_sources(self):
        self.assertEqual(len(catalog()['characters']),226)
        for sid,tag in [('H191','antiRevive'),('H182','extinction'),('H200','punishNonAttack'),('H204','pull')]:
            row=mechanics({'StaticID':sid,'LV':60})
            self.assertIn(tag,row['tags']);self.assertTrue(row['sources'][tag])
        row=mechanics({'StaticID':'H193','LV':60})
        self.assertTrue(any('四号位' in text for text in row['issues']))
        self.assertEqual(mechanics({'StaticID':'H192','LV':60})['scale'],'Defence')
        self.assertEqual(mechanics({'StaticID':'H197','LV':60})['scale'],'HP')
        self.assertFalse(mechanics({'StaticID':'H197','LV':60})['critical'])
        self.assertTrue(all(s['level'] is None for s in mechanics({'StaticID':'H182','LV':60},unknown=True)['skills']))

    def test_current_panel_matches_equipment_and_does_not_mutate_account(self):
        acc,roles=account_fixture();acc.client.account_state['Artifacts']={'Datas':[artifact('A0377')]}
        before=copy.deepcopy(acc.client.account_state);data=api.handle(acc,'overview',{})
        for row in data['roles']:
            gear,_=plans.worn_items(before['EquipmentContainer']['Datas'],row['id'])
            self.assertEqual(row['current']['panel'],stats.role_panel(before,roles[row['id']],gear,include_artifact_effects=True)['panel'])
            self.assertEqual(row['skills'][0]['level'],2)
        self.assertEqual(acc.client.account_state,before);acc.client.call.assert_not_called();acc.client.bootstrap.assert_not_called()

    def test_corrupt_duplicate_and_partial_assets_are_excluded(self):
        acc,_=account_fixture();state=acc.client.account_state
        state['EquipmentContainer']['Datas'].append(copy.deepcopy(state['EquipmentContainer']['Datas'][0]))
        state['EquipmentContainer']['Datas']=[e for e in state['EquipmentContainer']['Datas'] if plans._oid(e['_id'])!='r1-e5-0']
        state['Artifacts']={'Datas':[artifact('A0377'),artifact('A0377',aid='a1')]}
        data=adapters.roster(state);rows={r['id']:r for r in data['roles']}
        self.assertFalse(rows['r0']['ready']);self.assertFalse(rows['r1']['ready'])
        self.assertTrue(rows['r2']['ready'])


class PairingTests(unittest.TestCase):
    def assert_pair(self,plan,req):
        all_roles=[];gear=[];arts=[]
        for half in adapters.HALVES:
            row=plan['halves'][half];self.assertEqual(len(row['roles']),3)
            ids={r['id'] for r in row['roles']}
            self.assertTrue(set(req['options']['locks'][half])<=ids)
            self.assertFalse(set(req['options']['excluded'])&ids)
            all_roles.extend(row['roles']);gear.extend(eid for r in row['roles'] for eid in r['equipIds'])
            arts.extend(r['artifact']['id'] for r in row['roles'] if r['artifact'])
            self.assertTrue(all(c['status'] in ('pass','fail','unknown','not_applicable') for c in row['checks']))
            self.assertEqual(next(c for c in row['checks'] if c['id']=='kill')['status'],'unknown')
        self.assertEqual(len({r['id'] for r in all_roles}),6);self.assertEqual(len({r['staticID'] for r in all_roles}),6)
        self.assertEqual(len(set(gear)),36);self.assertEqual(len(set(arts)),len(arts))

    def test_100_reproducible_constraint_scenarios(self):
        acc,_=account_fixture(10);data=adapters.roster(acc.client.account_state)
        for i in range(100):
            with self.subTest(scenario=i):
                body=request();body['options']={'policy':'auto' if i%2 else 'manual','goal':['balanced','upper','lower'][i%3],
                    'locks':{'upper':['r'+str(i%5)],'lower':['r'+str(5+i%5)]},'excluded':['r'+str((i+1)%5)] if i%4==0 else []}
                for half in adapters.HALVES:
                    for j,enemy in enumerate(body['enemies'][half]):
                        enemy['speed']={'kind':'exact','value':100+(i*7+j*13)%300} if i%2 else {'kind':'lower','min':240+j*4}
                req=adapters.normalize(body);result=search.recommend(data,req)
                self.assertTrue(result['plans'])
                for plan in result['plans']:self.assert_pair(plan,req)
        self.assertEqual(search.recommend(data,req),search.recommend(data,req))

    def test_only_speed_lower_bound_never_claims_first_turn_and_current_hp_not_kill(self):
        acc,_=account_fixture();data=adapters.roster(acc.client.account_state);body=request()
        for enemy in body['enemies']['upper']:
            enemy.update(speed={'kind':'lower','min':100},health={'kind':'exact','context':'current','value':1})
        req=adapters.normalize(body);result=search.recommend(data,req)
        for plan in result['plans']:
            checks={c['id']:c for c in plan['halves']['upper']['checks']}
            self.assertEqual(checks['panel-speed']['status'],'unknown');self.assertEqual(checks['first-turn']['status'],'unknown')
            self.assertIn('最大生命资料不全',checks['kill']['reason']);self.assertEqual(checks['kill']['status'],'unknown')

    def test_speed_constraints_influence_candidate_order(self):
        acc,_=account_fixture();rows=adapters.roster(acc.client.account_state)['roles']
        # 功能完全相同，仅一队足以超过已知敌方速度；速度情报必须进入选择。
        slow=copy.deepcopy(rows[:3]);fast=copy.deepcopy(slow)
        for r in slow:r['tags']=[];r['current']['panel']['Speed']=150
        for r in fast:r['current']['panel']['Speed']=300
        enemy=search.threat(adapters.normalize(request())['enemies']['upper']);enemy.update(unknownSpeed=0,speedMax=250,speedMin=250)
        self.assertGreater(search._core(fast,enemy),search._core(slow,enemy))

    def test_hp_damage_no_critical_and_defence_damage_use_their_actual_axes(self):
        acc,_=account_fixture();rows=adapters.roster(acc.client.account_state)['roles']
        healer=next(r for r in rows if r['staticID']=='H197');before=search.output_index(healer)
        healer['current']['panel'].update(CriticalRate=100,CriticalDamageRate=350,Attack=99999)
        self.assertEqual(search.output_index(healer),before)
        defence=next(r for r in rows if r['staticID']=='H192');before=search.output_index(defence)
        defence['current']['panel']['Attack']=99999
        self.assertEqual(search.output_index(defence),before)

    def test_non_attack_counter_and_known_bond_hit_data_are_explained(self):
        acc,_=account_fixture();rows=adapters.roster(acc.client.account_state)['roles'][:3];body=request()
        body['enemies']['upper'][0].update(artifact='A0377',artifactLevel=15,stats={'ResistanceRate':150})
        req=adapters.normalize(body);value=search.assess(rows,req['enemies']['upper'],'manual')
        checks={r['id']:r for r in value['checks']}
        self.assertEqual(checks['nonattack-response']['status'],'unknown')
        self.assertIn('Lv15',checks['known-enemy-bonds']['reason'])
        if any({'control','strip','break'}&set(r['tags']) for r in rows):self.assertIn('hit-resist-panel',checks)
        self.assertEqual(value['strategy'],'谨慎反打')

    def test_shared_cleanser_and_duplicate_static_role_never_appear_twice(self):
        acc,_=account_fixture(8);data=adapters.roster(acc.client.account_state)
        data['roles'][-1]['staticID']=data['roles'][0]['staticID']
        req=adapters.normalize(request());result=search.recommend(data,req)
        self.assertTrue(result['plans'])
        for plan in result['plans']:self.assert_pair(plan,req)

    def test_cross_team_equipment_and_artifact_conflicts_are_rejected(self):
        acc,_=account_fixture(6);data=adapters.roster(acc.client.account_state);req=adapters.normalize(request())
        for kind in ('equipment','artifact'):
            corrupt=copy.deepcopy(data)
            if kind=='equipment':corrupt['roles'][1]['equipIds'][0]=corrupt['roles'][0]['equipIds'][0]
            else:
                for r in corrupt['roles'][:2]:r['artifact']={'id':'shared'}
            self.assertEqual(search.recommend(corrupt,req)['plans'],[])

    def test_missing_and_unusable_locks_and_small_roster_explain(self):
        acc,_=account_fixture(5);data=adapters.roster(acc.client.account_state);req=adapters.normalize(request())
        self.assertIn('不足 6',search.recommend(data,req)['reason'])
        for rid in ('foreign','r0'):
            req['options']['locks']['upper']=[rid];next(r for r in data['roles'] if r['id']=='r0')['ready']=False
            with self.assertRaises(GameError):search.recommend(data,req)

    def test_cancellation_has_no_partial_result(self):
        acc,_=account_fixture();data=adapters.roster(acc.client.account_state)
        with self.assertRaises(search.Cancelled):search.recommend(data,adapters.normalize(request()),cancelled=lambda:True)


class ApiTests(unittest.TestCase):
    def test_idempotent_request_conflict_account_isolation_and_expiry(self):
        acc,_=account_fixture();body={**request(),'requestId':'offline-request-0001'}
        first=api.handle(acc,'recommend',body);done=wait(acc,first);self.assertEqual(done['status'],'completed')
        same=api.handle(acc,'recommend',body);self.assertEqual(first['jobId'],same['jobId'])
        with self.assertRaises(GameError):api.handle(acc,'recommend',{**body,'target':'changed'})
        other,_=account_fixture()
        with self.assertRaises(GameError):api.handle(other,'status',{'jobId':first['jobId']})
        acc.client.account_state['RoleDataContainer']['Roles'][0]['Skills']['Skills'][0]['Level']=3
        expired=api.handle(acc,'status',{'jobId':first['jobId']});self.assertEqual(expired['status'],'expired');self.assertNotIn('result',expired)
        acc.client.call.assert_not_called();acc.client.bootstrap.assert_not_called()

    def test_compute_releases_account_lock_and_stop_discards_result(self):
        acc,_=account_fixture();started=threading.Event();release=threading.Event()
        original=search.recommend
        def slow(data,req,**kwargs):
            started.set();release.wait(2);return original(data,req,**kwargs)
        with patch.object(search,'recommend',slow):
            job=api.handle(acc,'recommend',{**request(),'requestId':'offline-cancel-0001'});self.assertTrue(started.wait(1))
            self.assertTrue(acc.lock.acquire(False));acc.lock.release()
            api.handle(acc,'cancel',{'jobId':job['jobId']});release.set();value=wait(acc,job)
        self.assertEqual(value['status'],'cancelled');self.assertNotIn('result',value)

    def test_export_uses_verified_job_sanitized_name_and_no_credentials(self):
        acc,_=account_fixture();body={**request(),'requestId':'offline-export-0001','target':'../对手'}
        value=wait(acc,api.handle(acc,'recommend',body));plan=value['result']['plans'][0]
        with tempfile.TemporaryDirectory() as directory,patch.object(api.config,'PLATFORM','windows'),patch.object(api.config,'get_download_directory',lambda:Path(directory)):
            result=api.handle(acc,'export',{'jobId':value['jobId'],'planId':plan['id']})
            path=Path(result['path']);self.assertEqual(path.parent,Path(directory));payload=json.loads(path.read_text())
            self.assertEqual(payload['format'],'lucima-gvg-plan');self.assertEqual(payload['snapshotHash'],value['snapshotHash'])
            self.assertNotIn('password',path.read_text());self.assertNotIn('token',path.read_text())
        with patch.object(api.config,'PLATFORM','android'):
            value=api.handle(acc,'export',{'jobId':value['jobId'],'planId':plan['id']})
            self.assertTrue(value['filename'].endswith('.json'));self.assertIn('content',value)

    def test_http_routing(self):
        from backend import server,appcore
        with patch.object(appcore,'gvg_ep',return_value={'ok':True}) as handle:
            self.assertEqual(server._route_api('POST','/api/gvg/action',{'account':'local','action':'overview'},{}),{'ok':True})
            handle.assert_called_once_with('local','overview',{'account':'local','action':'overview'})
        self.assertEqual(server._route_api('GET','/api/gvg/catalog',{}, {})['versions'],versions())


if __name__=='__main__':unittest.main()
