"""Destructive inventory decisions tested against an authoritative fake server."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend import inventory_management as service
from backend.game_client import GameError
from backend.tasks import _oid


def role(index, sid='R3', **fields):
    return {'_id': f'r{index}', 'StaticID': sid, 'Star': 6, 'LV': 1, 'Exp': 0,
            'ImprintLV': 0, 'IsLock': False, 'Locks': [], **fields}


def bond(index, sid='A3', **fields):
    # Native ArtifactData starts at LV=0; this differs from RoleData.LV=1.
    return {'_id': f'a{index}', 'StaticID': sid, 'LV': 0, 'Exp': 0, 'Enhance': 0,
            'IsLock': False, 'EquipRole': '', **fields}


CATALOG = {'roles': {sid: {'star': star, 'sell': True} for sid, star in [('R3',3),('R4',4),('R5',5),('H064',3)]},
           'bonds': {sid: {'star': star, 'sell': True} for sid, star in [('A3',3),('A4',4),('A5',5)]}}


class Client:
    def __init__(self, roles=(), bonds=()):
        self.server = {'RoleDataContainer': {'Roles': deepcopy(list(roles))}, 'Artifacts': {'Datas': deepcopy(list(bonds))}}
        self.account_state = deepcopy(self.server)
        self.calls = []; self.lose_response = False; self.reject_lock = False
    def _auth_data(self, body):return body
    def bootstrap(self):self.account_state=deepcopy(self.server)
    def call(self, route, body):
        self.calls.append((route,deepcopy(body)))
        kind = 'roles' if route.startswith('RoleHandler.') else 'bonds'
        rows = self.server['RoleDataContainer']['Roles'] if kind=='roles' else self.server['Artifacts']['Datas']
        if 'Lock' in route:
            target = next(r for r in rows if r['_id'] == (body['RoleID'] if kind=='roles' else body['ArtifactID']))
            if not self.reject_lock:target['IsLock']=not target['IsLock']
            result={'Roles':[deepcopy(target)]} if kind=='roles' else {'NewArtifact':deepcopy(target)}
        elif 'Imprint' in route or 'Upgrade' in route:
            target=next(r for r in rows if r['_id']==body.get('UpgradRoleID',body.get('ArtifactID')))
            identifier=body['MaterialRoleIDs'][0] if kind=='roles' else body['Materials'][0]['Artifact']['_id']
            donor=next(r for r in rows if r['_id']==identifier)
            assert not donor['IsLock']
            field='ImprintLV' if kind=='roles' else 'Enhance'
            minimum=max(0,CATALOG['roles'][target['StaticID']]['star']-3) if kind=='roles' else 0
            value=donor[field]-minimum+1 if donor[field] else 1
            target[field]=max(target[field],minimum)+value
            assert target[field] <= (7 if kind=='roles' else 5)
            if kind=='bonds':
                # Feeding also grants experience. Current LV must not decide
                # whether another copy is needed to raise the level cap.
                target['LV']=min(30,target['LV']+1)
                target['Exp']=355
            rows.remove(donor)
            result=({'Roles':[deepcopy(target)],'RemoveRoleIDs':[identifier]} if kind=='roles' else
                    {'Artifact':deepcopy(target),'CostItems':[{'Artifact':deepcopy(donor)}]})
        else:
            identifiers=body['RoleIDs'] if kind=='roles' else [r['Artifact']['_id']for r in body['CostItems']]
            removed=[r for r in rows if r['_id']in identifiers]
            assert all(not r['IsLock']for r in removed)
            if kind=='bonds':assert all(r['LV']==0 and not r['EquipRole']for r in removed)
            for r in removed:rows.remove(r)
            result={'Drop':{'Items':[{'Item':{'StaticID':'1','Count':len(removed)*100}}]},
                    'RemoveRoleIDs':identifiers} if kind=='roles' else {
                    'Drop':{'Items':[{'Item':{'StaticID':'1','Count':len(removed)*100}}]},'CostItems':[{'Artifact':r}for r in removed]}
        if self.lose_response:raise TimeoutError('reply lost')
        return result


class LibraryClient(Client):
    def __init__(self, roles=(), bonds=(), library=()):
        super().__init__(roles,bonds)
        self.account_state['AccountSaveData']={'RolePackMaxCount':4,'ArtifactPackMaxCount':4}
        self.server['AccountSaveData']=deepcopy(self.account_state['AccountSaveData'])
        self.account_state['LibraryContainer']={'LibraryDataList':deepcopy(list(library))}
        self.server['LibraryContainer']=deepcopy(self.account_state['LibraryContainer'])
        self.next_taken = 0
        self.take_snapshots = []
    def bootstrap(self): self.account_state=deepcopy(self.server)
    def call(self,route,body):
        if route=='LibraryHandler.TakeOutLibrary':
            self.calls.append((route,deepcopy(body)))
            picks=body['LibraryDatas']
            kind='roles' if picks[0]['ItemData']['StaticID'] in CATALOG['roles'] else 'bonds'
            bag=self.server['RoleDataContainer']['Roles'] if kind=='roles' else self.server['Artifacts']['Datas']
            capacity=self.server['AccountSaveData']['RolePackMaxCount' if kind=='roles' else 'ArtifactPackMaxCount']
            used=len(bag) if kind=='roles' else sum(not _oid(r.get('EquipRole')) for r in bag)
            count=sum(pick['ItemData']['Count'] for pick in picks)
            self.take_snapshots.append({'count':count,'space':capacity-used})
            assert 0 < count <= min(40,capacity-used)
            library=self.server['LibraryContainer']['LibraryDataList']
            for pick in picks:
                row=next(r for r in library if r['_id']==pick['_id']);count=pick['ItemData']['Count']
                for _ in range(count):
                    self.next_taken += 1
                    bag.append({'_id':f'taken-{self.next_taken}','StaticID':pick['ItemData']['StaticID'],
                                'IsLock':False,'ImprintLV':0,'LV':1 if kind=='roles' else 0,
                                'Exp':0,'Enhance':0,'EquipRole':''})
                row['ItemData']['Count']-=count
                if row['ItemData']['Count']==0:library.remove(row)
            if self.lose_response:raise TimeoutError('reply lost')
            return {'Roles':deepcopy(bag)}
        return super().call(route,body)


class InventoryManagement(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(service,'catalog',return_value=CATALOG);self.patch.start();self.addCleanup(self.patch.stop)

    def test_initial_star_controls_eligibility_and_small_water_shield_is_untouched(self):
        client=Client([role(i)for i in range(10)]+[role(20,'R5',Star=3),role(21,'H064')])
        result=service.run(client,'roles')
        self.assertEqual((result['locked'],result['upgraded'],result['sold']),(1,7,2))
        remaining=service.rows(client,'roles')
        self.assertEqual({r['StaticID']for r in remaining},{'R3','R5','H064'})
        self.assertEqual(next(r for r in remaining if r['StaticID']=='R3')['ImprintLV'],7)
        self.assertFalse(next(r for r in remaining if r['StaticID']=='H064')['IsLock'])

    def test_four_star_imprint_starts_at_native_minimum(self):
        client=Client([role(i,'R4')for i in range(9)])
        result=service.run(client,'roles')
        self.assertEqual((result['upgraded'],result['sold']),(6,2))

    def test_every_manually_locked_character_is_kept_and_filled_before_sale(self):
        client=Client([role(0,IsLock=True,ImprintLV=6),role(1,IsLock=True,ImprintLV=5)]+[role(i)for i in range(2,7)])
        result=service.run(client,'roles')
        self.assertEqual((result['locked'],result['upgraded'],result['sold']),(0,3,2))
        self.assertEqual(len(service.rows(client,'roles')),2)

    def test_insufficient_materials_does_not_sell_or_consume_protected_characters(self):
        client=Client([role(0,IsLock=True),role(1,Locks=['Team']),role(2,IsDispatched=True),
                       role(3,IsFavorite=True),role(4,EquipmentMap={'Weapon':{'_id':'e'}})])
        result=service.run(client,'roles');self.assertEqual(result['sold'],0);self.assertEqual(result['upgraded'],0)

    def test_bonds_lock_two_fill_both_then_sell_only_untrained_copies(self):
        client=Client(bonds=[bond(i)for i in range(15)]+[bond(50,LV=9,IsLock=True,Enhance=5),bond(51,'A5')])
        result=service.run(client,'bonds')
        self.assertEqual((result['locked'],result['upgraded'],result['sold']),(1,5,9))
        self.assertEqual(len(service.rows(client,'bonds')),3)

    def test_more_manual_locks_than_quota_are_all_filled(self):
        client=Client(bonds=[bond(i,IsLock=True,Enhance=4)for i in range(3)]+[bond(i)for i in range(3,8)])
        result=service.run(client,'bonds',{'keep3':2,'keep4':1})
        self.assertEqual((result['locked'],result['upgraded'],result['sold']),(0,3,2))
        self.assertTrue(all(r['IsLock']and r['Enhance']==5 for r in service.rows(client,'bonds')))

    def test_bond_quota_star_settings_are_independent(self):
        client=Client(bonds=[bond(i,'A3',Enhance=5)for i in range(4)]+[bond(i+10,'A4',Enhance=5)for i in range(4)])
        result=service.run(client,'bonds',{'keep3':3,'keep4':1})
        self.assertEqual(result['locked'],4);self.assertEqual(result['sold'],4)
        self.assertEqual([r['StaticID']for r in service.rows(client,'bonds')].count('A3'),3)

    def test_levelled_equipped_unknown_lock_and_unfinished_bonds_are_never_sold(self):
        client=Client(bonds=[bond(0,IsLock=True,Enhance=5),bond(1,IsLock=True,Enhance=5),
             bond(2,LV=1),bond(3,EquipRole='r'),bond(4,IsLock=None),bond(5,Exp=7)])
        self.assertEqual(service.run(client,'bonds')['sold'],0)
        client=Client(bonds=[bond(0,IsLock=True,Enhance=3),bond(1,IsLock=True,Enhance=5),bond(2)])
        result=service.run(client,'bonds');self.assertEqual(result['sold'],0)

    def test_native_zero_level_duplicates_fill_two_locked_bonds_before_any_sale(self):
        bases=[bond(0,IsLock=True),bond(1,IsLock=True)]
        client=Client(bonds=bases+[bond(i) for i in range(2,14)])
        result=service.run(client,'bonds')
        self.assertEqual((result['locked'],result['upgraded'],result['sold']),(0,10,2))
        self.assertEqual([route for route,_ in client.calls],
                         ['ArtifactHandler.Upgrade']*10+['ArtifactHandler.SellArtifacts'])
        remaining=service.rows(client,'bonds')
        self.assertEqual({r['_id'] for r in remaining},{'a0','a1'})
        self.assertTrue(all(r['Enhance']==5 and 15+r['Enhance']*3==30 for r in remaining))
        self.assertTrue(all(r['LV']<30 for r in remaining))
        self.assertTrue(all(body['Materials'][0]['Artifact']['LV']==0
                            for route,body in client.calls if route=='ArtifactHandler.Upgrade'))

    def test_current_level_thirty_without_full_breakthrough_still_needs_duplicates(self):
        client=Client(bonds=[bond(0,IsLock=True,LV=30)]+[bond(i) for i in range(1,7)])
        result=service.run(client,'bonds',{'keep3':1})
        self.assertEqual((result['upgraded'],result['sold']),(5,1))
        self.assertEqual(service.rows(client,'bonds')[0]['Enhance'],5)

    def test_full_breakthrough_does_not_need_current_level_thirty(self):
        client=Client(bonds=[bond(0,IsLock=True,Enhance=5),bond(1,IsLock=True,Enhance=5),bond(2)])
        result=service.run(client,'bonds')
        self.assertEqual((result['upgraded'],result['sold']),(0,1))
        self.assertTrue(all(r['LV']==0 for r in service.rows(client,'bonds')))

    def test_native_duplicates_free_overfull_bag_and_finish_breakthrough_across_withdrawals(self):
        client=LibraryClient(bonds=[bond(0,IsLock=True),bond(1,IsLock=True),bond(2),bond(3),bond(4)],
            library=[{'_id':'lib1','ItemData':{'StaticID':'A3','Count':10}}])
        result=service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertTrue(result['ok']);self.assertEqual(result['libraryRemaining'],0)
        self.assertEqual((result['upgraded'],result['sold'],result['retrieved']),(10,3,10))
        remaining=service.rows(client,'bonds')
        self.assertTrue(all(r['IsLock'] and r['Enhance']==5 and r['LV']<30 for r in remaining))
        self.assertEqual(len(remaining),2)
        routes=[route for route,_ in client.calls]
        self.assertEqual(routes[:3],['ArtifactHandler.Upgrade']*3)
        self.assertLess(routes.index('ArtifactHandler.Upgrade'),routes.index('LibraryHandler.TakeOutLibrary'))

    def test_rejected_lock_stops_before_any_consumption(self):
        client=Client([role(i)for i in range(10)]);client.reject_lock=True
        with self.assertRaises(GameError):service.run(client,'roles')
        self.assertEqual(len(client.calls),1)

    def test_lost_reply_is_not_retried_and_next_run_syncs_actual_lock(self):
        client=Client([role(0)]);client.lose_response=True
        with self.assertRaises(TimeoutError):service.run(client,'roles')
        self.assertEqual(len(client.calls),1)
        client.lose_response=False
        result=service.run(client,'roles')
        self.assertEqual(result['locked'],0);self.assertTrue(service.rows(client,'roles')[0]['IsLock'])

    def test_partial_confirmation_requires_authoritative_refresh(self):
        client=Client([role(0,IsLock=True,ImprintLV=7),role(1)])
        original=client.call
        def omit(route,body):
            response=original(route,body);response.pop('RemoveRoleIDs',None);return response
        client.call=omit
        self.assertEqual(service.run(client,'roles')['sold'],1)
        self.assertEqual(len(service.rows(client,'roles')),1)

    def test_checked_library_settles_bag_before_each_retrieval(self):
        library=[{'_id':'lib1','ItemData':{'StaticID':'R3','Count':5}}]
        client=LibraryClient([role(0)],library=library)
        result=service.run(client,'roles',{'retrieveLibrary':True})
        self.assertEqual(result['retrieved'],5);self.assertEqual(result['libraryRemaining'],0)
        routes=[route for route,_ in client.calls]
        first_take=routes.index('LibraryHandler.TakeOutLibrary')
        self.assertTrue(any(route in ('RoleHandler.LockRole','RoleHandler.ImprintRole')
                            for route in routes[:first_take]))

    def test_full_bond_bag_sells_before_each_library_retrieval(self):
        ready=[bond(0,IsLock=True,Enhance=5),bond(1,IsLock=True,Enhance=5)]
        library=[{'_id':'lib1','ItemData':{'StaticID':'A3','Count':3}}]
        client=LibraryClient(bonds=ready+[bond(2),bond(3,'A5')],library=library)
        result=service.run(client,'bonds',{'keep3':2,'keep4':2,'retrieveLibrary':True})
        self.assertFalse(result['blocked']);self.assertEqual(result['libraryRemaining'],0)
        self.assertEqual((result['sold'],result['retrieved'],result['rounds']),(4,3,4))
        routes=[route for route,_ in client.calls]
        sale='ArtifactHandler.SellArtifacts';take='LibraryHandler.TakeOutLibrary'
        self.assertEqual(routes,[sale,take,sale,take,sale,take,sale])
        self.assertEqual(client.take_snapshots,[{'count':1,'space':1}]*3)
        self.assertEqual({r['_id'] for r in service.rows(client,'bonds')},{'a0','a1','a3'})

    def test_third_full_three_star_bond_is_sold_before_retrieval_even_with_free_slots(self):
        client=LibraryClient(bonds=[bond(i,Enhance=5) for i in range(3)],
                             library=[{'_id':'lib1','ItemData':{'StaticID':'A3','Count':1}}])
        result=service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertEqual((result['locked'],result['sold'],result['retrieved']),(2,2,1))
        routes=[route for route,_ in client.calls]
        self.assertEqual(routes,['ArtifactHandler.Lock','ArtifactHandler.Lock',
                                'ArtifactHandler.SellArtifacts','LibraryHandler.TakeOutLibrary',
                                'ArtifactHandler.SellArtifacts'])
        self.assertEqual(client.take_snapshots,[{'count':1,'space':2}])
        self.assertEqual(len(service.rows(client,'bonds')),2)

    def test_full_role_bag_transfers_spare_before_each_retrieval(self):
        client=LibraryClient([role(0,IsLock=True,ImprintLV=7),role(1),role(2,'R5'),role(3,'H064')],
                             library=[{'_id':'lib1','ItemData':{'StaticID':'R3','Count':3}}])
        result=service.run(client,'roles',{'retrieveLibrary':True})
        self.assertTrue(result['ok']);self.assertEqual(result['libraryRemaining'],0)
        self.assertEqual((result['sold'],result['retrieved'],result['rounds']),(4,3,4))
        sale='RoleHandler.SellRoles';take='LibraryHandler.TakeOutLibrary'
        self.assertEqual([route for route,_ in client.calls],[sale,take,sale,take,sale,take,sale])
        self.assertEqual(client.take_snapshots,[{'count':1,'space':1}]*3)
        self.assertEqual({r['_id'] for r in service.rows(client,'roles')},{'r0','r2','r3'})

    def test_library_fills_bag_in_batches_of_at_most_forty_then_repeats_tidy(self):
        for kind in ('roles','bonds'):
            with self.subTest(kind=kind):
                if kind=='roles':
                    client=LibraryClient([role(0,IsLock=True,ImprintLV=7),role(1)],
                        library=[{'_id':'lib1','ItemData':{'StaticID':'R3','Count':170}}])
                    client.server['AccountSaveData']['RolePackMaxCount']=84
                    sale='RoleHandler.SellRoles';base_count=1
                else:
                    client=LibraryClient(bonds=[bond(0,IsLock=True,Enhance=5),bond(1,IsLock=True,Enhance=5),bond(2)],
                        library=[{'_id':'lib1','ItemData':{'StaticID':'A3','Count':170}}])
                    client.server['AccountSaveData']['ArtifactPackMaxCount']=85
                    sale='ArtifactHandler.SellArtifacts';base_count=2
                result=service.run(client,kind,{'retrieveLibrary':True})
                self.assertTrue(result['ok']);self.assertEqual(result['libraryRemaining'],0)
                self.assertEqual((result['sold'],result['retrieved'],result['rounds']),(171,170,4))
                self.assertEqual([r['count'] for r in client.take_snapshots],[40,40,3,40,40,3,4])
                self.assertEqual(len(service.rows(client,kind)),base_count)
                # Each full bag is settled before another set of withdrawals.
                actions=['take' if route=='LibraryHandler.TakeOutLibrary' else 'sale'
                         for route,_ in client.calls if route in (sale,'LibraryHandler.TakeOutLibrary')]
                self.assertEqual(actions,['sale','take','take','take']+['sale']*5+
                                 ['take','take','take']+['sale']*5+['take','sale'])

    def test_full_protected_bag_stops_without_withdrawing_or_unlocking(self):
        for kind in ('roles','bonds'):
            with self.subTest(kind=kind):
                if kind=='roles':
                    client=LibraryClient([role(i,IsLock=True,ImprintLV=7) for i in range(4)],
                        library=[{'_id':'lib1','ItemData':{'StaticID':'R3','Count':3}}])
                else:
                    client=LibraryClient(bonds=[bond(i,IsLock=True,Enhance=5) for i in range(4)],
                        library=[{'_id':'lib1','ItemData':{'StaticID':'A3','Count':3}}])
                result=service.run(client,kind,{'retrieveLibrary':True})
                self.assertFalse(result['ok']);self.assertTrue(result['blocked'])
                self.assertEqual((result['sold'],result['retrieved'],result['libraryRemaining']),(0,0,3))
                self.assertIn('无法继续腾出空间',result['detail']);self.assertEqual(client.calls,[])

    def test_stop_when_retrieved_protected_cards_fill_remaining_slots(self):
        client=LibraryClient(bonds=[bond(0,IsLock=True,Enhance=5),bond(1,IsLock=True,Enhance=5),bond(2)],
            library=[{'_id':'lib1','ItemData':{'StaticID':'A5','Count':3}}])
        result=service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertFalse(result['ok']);self.assertTrue(result['blocked'])
        self.assertEqual((result['sold'],result['retrieved'],result['libraryRemaining']),(1,2,1))
        self.assertEqual([route for route,_ in client.calls],
                         ['ArtifactHandler.SellArtifacts','LibraryHandler.TakeOutLibrary'])

    def test_unchecked_library_only_settles_bag_without_requiring_warehouse_or_capacity(self):
        client=Client(bonds=[bond(i,Enhance=5) for i in range(3)])
        result=service.run(client,'bonds')
        self.assertTrue(result['ok'])
        self.assertEqual((result['sold'],result['retrieved'],result['rounds']),(1,0,1))
        self.assertFalse(any(route=='LibraryHandler.TakeOutLibrary' for route,_ in client.calls))

    def test_lost_retrieval_reply_stops_and_resume_uses_fresh_inventory(self):
        client=LibraryClient(bonds=[bond(0,IsLock=True,Enhance=5),bond(1,IsLock=True,Enhance=5)],
            library=[{'_id':'lib1','ItemData':{'StaticID':'A3','Count':3}}])
        client.lose_response=True
        with self.assertRaises(TimeoutError):service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertTrue(client._inventory_management_uncertain)
        self.assertEqual([route for route,_ in client.calls],['LibraryHandler.TakeOutLibrary'])
        client.lose_response=False
        result=service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertTrue(result['ok'])
        self.assertEqual((result['sold'],result['retrieved'],result['libraryRemaining']),(3,1,0))
        self.assertEqual([route for route,_ in client.calls],['LibraryHandler.TakeOutLibrary',
            'ArtifactHandler.SellArtifacts','LibraryHandler.TakeOutLibrary','ArtifactHandler.SellArtifacts'])

    def test_unconfirmed_retrieval_stops_before_any_sale(self):
        client=LibraryClient(bonds=[bond(0,IsLock=True,Enhance=5),bond(1,IsLock=True,Enhance=5)],
            library=[{'_id':'lib1','ItemData':{'StaticID':'A3','Count':3}}])
        original=client.call
        def incomplete(route,body):
            result=original(route,body)
            if route=='LibraryHandler.TakeOutLibrary':client.server['Artifacts']['Datas'].pop()
            return result
        client.call=incomplete
        with self.assertRaisesRegex(GameError,'到账未能核对'):
            service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertTrue(client._inventory_management_uncertain)
        self.assertEqual([route for route,_ in client.calls],['LibraryHandler.TakeOutLibrary'])

    def test_checked_library_retries_after_tidy_frees_bag_space(self):
        library=[{'_id':'lib1','ItemData':{'StaticID':'R3','Count':5}}]
        client=LibraryClient([role(i) for i in range(4)],library=library)
        result=service.run(client,'roles',{'retrieveLibrary':True})
        self.assertFalse(result['blocked']);self.assertEqual(result['libraryRemaining'],0);self.assertEqual(result['retrieved'],5)

    def test_equipped_bonds_do_not_consume_backpack_slots(self):
        worn=[bond(i,'A5',EquipRole={'$oid':f'holder-{i}'}) for i in range(5)]
        client=LibraryClient(bonds=worn+[bond(10,'A5'),bond(11,'A5')],
            library=[{'_id':'lib1','ItemData':{'StaticID':'A5','Count':2}}])
        result=service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertTrue(result['ok']);self.assertEqual(result['retrieved'],2)
        self.assertEqual(result['libraryRemaining'],0)
        self.assertEqual(client.take_snapshots,[{'count':2,'space':2}])
        self.assertEqual(len(service.rows(client,'bonds')),9)
        self.assertEqual([route for route,_ in client.calls],['LibraryHandler.TakeOutLibrary'])

    def test_latest_expanded_capacity_is_loaded_before_counting_unworn_bonds(self):
        worn=[bond(i,'A5',EquipRole=f'holder-{i}') for i in range(44)]
        client=LibraryClient(bonds=worn+[bond(i+100,'A5') for i in range(115)],
            library=[{'_id':'lib1','ItemData':{'StaticID':'A5','Count':5}}])
        client.account_state['AccountSaveData']['ArtifactPackMaxCount']=115
        client.server['AccountSaveData']['ArtifactPackMaxCount']=120
        result=service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertTrue(result['ok']);self.assertEqual(result['retrieved'],5)
        self.assertEqual(client.take_snapshots,[{'count':5,'space':5}])
        self.assertEqual(result['bag'],{'count':120,'capacity':120,'space':0})
        self.assertIn('120/120',result['detail'])

    def test_equipped_locked_bases_keep_all_slots_available_for_sale_retrieval_cycles(self):
        ready=[bond(0,IsLock=True,Enhance=5,EquipRole='r0'),
               bond(1,IsLock=True,Enhance=5,EquipRole='r1')]
        client=LibraryClient(bonds=ready+[bond(i) for i in range(2,6)],
            library=[{'_id':'lib1','ItemData':{'StaticID':'A3','Count':10}}])
        result=service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertTrue(result['ok'])
        self.assertEqual((result['sold'],result['retrieved']),(14,10))
        self.assertEqual(client.take_snapshots,[{'count':4,'space':4},{'count':4,'space':4},
                                              {'count':2,'space':4}])
        self.assertEqual({r['_id'] for r in service.rows(client,'bonds')},{'a0','a1'})

    def test_roles_with_equipment_still_count_toward_role_capacity(self):
        client=LibraryClient([role(i,'R5',ArtifactData=bond(i),EquipmentMap={'Weapon':'e'}) for i in range(4)],
            library=[{'_id':'lib1','ItemData':{'StaticID':'R5','Count':1}}])
        result=service.run(client,'roles',{'retrieveLibrary':True})
        self.assertTrue(result['blocked']);self.assertEqual(result['retrieved'],0)
        self.assertIn('4/4',result['detail']);self.assertIn('整理暂停',result['detail'])
        self.assertEqual(client.calls,[])

    def test_equipped_bonds_do_not_hide_a_genuinely_full_unworn_bag(self):
        client=LibraryClient(bonds=[bond(i,'A5',EquipRole='r') for i in range(5)]+
                                  [bond(i+10,'A5') for i in range(4)],
            library=[{'_id':'lib1','ItemData':{'StaticID':'A5','Count':1}}])
        result=service.run(client,'bonds',{'retrieveLibrary':True})
        self.assertTrue(result['blocked']);self.assertEqual(result['retrieved'],0)
        self.assertIn('4/4',result['detail']);self.assertIn('空位 0',result['detail'])
        self.assertEqual(client.calls,[])

    def test_card_snapshot_reports_each_inventory_without_equipped_bonds_or_mutations(self):
        client=LibraryClient([role(0),role(1)],bonds=[bond(i,'A5') for i in range(118)]+
            [bond(i+200,'A5',EquipRole='r0') for i in range(5)],
            library=[{'_id':'roles','ItemData':{'StaticID':'R3','Count':7}},
                     {'_id':'bonds','ItemData':{'StaticID':'A3','Count':0}}])
        client.server['AccountSaveData'].update(RolePackMaxCount=200,ArtifactPackMaxCount=145)
        before=deepcopy(client.server)
        result=service.snapshot(client.server)
        self.assertEqual(result['roles'],{'count':2,'capacity':200,'space':198,'library':7})
        self.assertEqual(result['bonds'],{'count':118,'capacity':145,'space':27,'library':0})
        self.assertEqual(client.server,before);self.assertEqual(client.calls,[])
        from backend.appcore import summarize
        self.assertEqual(summarize(client.server)['inventoryManagement'],result)

    def test_card_snapshot_does_not_claim_zero_for_missing_capacity_inventory_or_library(self):
        result=service.snapshot({})
        self.assertTrue(all(value is None for row in result.values() for value in row.values()))

    def test_only_same_name_copies_feed_locked_bases_and_excess_is_sold_not_used_for_experience(self):
        initial=[bond(0,IsLock=True,Enhance=4),bond(1,IsLock=True,Enhance=4)]+[bond(i) for i in range(2,5)]
        initial += [bond(i,'A4',IsLock=True,Enhance=4) for i in (10,11)]+[bond(i,'A4') for i in range(12,15)]
        initial += [bond(i,'A5') for i in range(20,24)]
        static_ids={r['_id']:r['StaticID'] for r in initial}
        client=Client(bonds=initial)
        result=service.run(client,'bonds')
        self.assertEqual((result['upgraded'],result['levelingConsumed'],result['sold']),(4,0,2))
        self.assertIn('突破 4 次，升级 0 个',result['detail'])
        for route,body in client.calls:
            if route=='ArtifactHandler.Upgrade':
                self.assertEqual(set(body),{'ArtifactID','Materials'})
                self.assertEqual(len(body['Materials']),1)
                self.assertEqual(set(body['Materials'][0]),{'Artifact'})
                donor=body['Materials'][0]['Artifact']
                self.assertEqual(static_ids[body['ArtifactID']],donor['StaticID'])
                self.assertNotIn(donor['_id'],{'a4','a14','a20','a21','a22','a23'})
        self.assertEqual(len([r for r in service.rows(client,'bonds') if r['StaticID']=='A5']),4)

    def test_equipped_bond_lock_carries_owner_and_upgrade_updates_role_copy(self):
        worn=bond(0,EquipRole='r0',Enhance=4)
        client=Client([role(0,ArtifactData=deepcopy(worn))],bonds=[worn,bond(1)])
        result=service.run(client,'bonds',{'keep3':1})
        self.assertEqual(result['upgraded'],1)
        self.assertEqual(client.calls[0][1],{'RoleID':'r0','ArtifactID':'a0'})
        self.assertEqual(service.rows(client,'roles')[0]['ArtifactData']['Enhance'],5)


if __name__=='__main__':unittest.main()
