"""Native EquipmentSaveData.LV, incremental drops and early-stop sales."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from backend import equipment_sale as sales,tasks
from backend.game_client import GameMessage


def native_equipment(index=0, *, sid='E0051', enhancement=0, tier=0, **fields):
    # The native response has LV, not Enhance/EnhanceLevel. StaticID supplies
    # the item's base level; E0051 is a level-51 weapon.
    return {'_id':{'$oid':f'native-{index}'},'StaticID':sid,'LV':enhancement,'Exp':0,
            'ClassLV':tier,'IsLock':False,'IsNew':True,'Set':'Attack',
            'MainProp':{'PropertyType':'AttackValue','Value':61},
            'SubProps':{'SourceValues':[{'PropertyType':'AttackRate','Value':.03}]},**fields}


class NativeInventoryClient:
    def __init__(self,scene_id='BH806_1_1'):
        self.account_state={'EquipmentContainer':{'Datas':[]},
            'Teams':{'Settings':[{'ID':'T','TeamSetting':{'RolePosMap':{'r':'0'}}}]},
            'SceneDataContainer':{'Scenes':[{'StaticID':scene_id}]}}
        self.calls=[];self.battles=0;self.refreshes=0
    def _auth_data(self,body):return body
    def bootstrap(self):self.refreshes+=1;raise AssertionError('sale needs no relogin')
    def call(self,route,body):
        self.calls.append((route,deepcopy(body)))
        if route=='SceneHandler.FinishScene':
            self.battles+=1
            if self.battles==9:raise GameMessage('能源*24 數量不足 目前只剩:13')
            return {'Drop':{'Items':[{'Equipment':native_equipment(self.battles,tier=self.battles%4)},
                                   {'Item':{'StaticID':'1','Count':3031}}]}}
        if route=='EquipmentHandler.SellEquips':
            return {'Drop':{'Items':[{'Item':{'StaticID':'1','Count':len(body['IDs'])*50}},
                                    {'Item':{'StaticID':'EC14','Count':len(body['IDs'])}}]}}
        raise AssertionError(route)


class NativeSaleTests(unittest.TestCase):
    def test_native_zero_enhancement_is_sold_across_low_level_tiers(self):
        refs=tasks._equip_ref()
        self.assertEqual(refs['E0051']['level'],51)
        for tier in range(5):
            with self.subTest(tier=tier):self.assertTrue(sales.eligible(native_equipment(tier=tier),refs))
        self.assertFalse(sales.eligible(native_equipment(enhancement=1),refs))
        self.assertFalse(sales.eligible(native_equipment(enhancement=15),refs))

    def test_native_epic85_is_opt_in_and_protections_still_apply(self):
        refs=tasks._equip_ref();self.assertEqual(refs['E0101']['level'],85)
        epic=native_equipment(sid='E0101',tier=3)
        self.assertFalse(sales.eligible(epic,refs))
        self.assertTrue(sales.eligible(epic,refs,include_epic85=True))
        for patch_values in ({'LV':2},{'LV':False},{'LV':None},{'LV':'broken'},
                             {'Enhance':3},{'EnhanceLevel':1},{'IsLock':True},
                             {'EquipRole':'owned'},{'ClassLV':4}):
            with self.subTest(fields=patch_values):
                self.assertFalse(sales.eligible({**epic,**patch_values},refs,include_epic85=True))
        missing=deepcopy(epic);missing.pop('LV')
        self.assertFalse(sales.eligible(missing,refs,include_epic85=True))

    def test_eight_51_level_drops_sell_after_ninth_battle_stamina_failure(self):
        client=NativeInventoryClient()
        self.assertEqual(sales.sell(client,include_epic85=True)['sold'],0)
        with patch.object(tasks,'_build_role_for_battle',return_value={}):
            for _ in range(8):
                result=tasks._finish_scene(client,'BH806_1_1','T',quick_battle=False,fail_label='活动关卡')
                self.assertTrue(result['ok']);self.assertIn('51级',result['detail'])
            stopped=tasks._finish_scene(client,'BH806_1_1','T',quick_battle=False,fail_label='活动关卡')
        self.assertFalse(stopped['ok'])
        self.assertEqual(len(client.account_state['EquipmentContainer']['Datas']),8)
        result=sales.sell(client,include_epic85=True)
        self.assertTrue(result['ok'],result);self.assertEqual(result['sold'],8)
        self.assertEqual(client.calls[-1][0],'EquipmentHandler.SellEquips')
        self.assertEqual(client.calls[-1][1]['IDs'],[f'native-{i}'for i in range(1,9)])
        self.assertEqual(client.refreshes,0)
        self.assertEqual(client.account_state['EquipmentContainer']['Datas'],[])
        self.assertEqual({row['id']:row['count']for row in result['rewardSummary']},{'1':400,'EC14':8})

    def test_hunt_native_drops_sell_after_early_stamina_stop(self):
        for scene in ('HuntFire_11','ElfFire_4'):
            with self.subTest(scene=scene):
                client=NativeInventoryClient(scene)
                with patch.object(tasks,'_build_role_for_battle',return_value={}):
                    results=[tasks.sweep_once(client,scene,'T',False) for _ in range(9)]
                self.assertTrue(all(row['ok']for row in results[:8]));self.assertFalse(results[-1]['ok'])
                outcome=sales.sell(client)
                self.assertTrue(outcome['ok']);self.assertEqual(outcome['sold'],8)
                self.assertEqual(client.account_state['EquipmentContainer']['Datas'],[])


if __name__=='__main__':unittest.main()
