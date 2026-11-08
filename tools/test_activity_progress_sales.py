"""Automatic map progression shares sweep eligibility and confirmed sale income."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend import activity_progress as ap, abyss_loadout, config, equipment_sale, tasks
from tools import test_activity_progress as fixtures
from tools.test_equipment_sale import equip, drop, REFS


class ProgressSales(unittest.TestCase):
    finish = fixtures.ActivityProgressTests.finish
    battle = fixtures.ActivityProgressTests.battle
    elite_sequence = fixtures.ActivityProgressTests.elite_sequence
    clear = fixtures.ActivityProgressTests.clear

    def setUp(self):
        fixtures.ActivityProgressTests.setUp(self)
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(config, 'data_dir', return_value=Path(temp.name)))
        self.enterContext(patch.object(tasks, '_equip_ref', return_value=REFS))
        self.client.login_id = 'sale-offline'
        self.client.account_state['EquipmentContainer'] = {'Datas': [
            equip(0), equip(1, tier=3), equip(2, tier=4), equip(3, IsLock=True),
            equip(4, Enhance=1), equip(5, EquipRole='hero'), equip(6)]}
        self.client._automation_reserved_equipment = {'e6'}
        self.sales_at = []; self.sale_ids = []; self.events = []
        self.client.call.side_effect = self.call

    def call(self, route, payload):
        if route == 'EquipmentHandler.SellEquips':
            self.sales_at.append(sum(row['cleared'] for row in ap.scenes(self.client.account_state, 'BH610')))
            self.sale_ids.extend(payload['IDs'])
            return drop(100 * len(payload['IDs']))
        reward = self.finish(route, payload)
        gear = equip('stage-' + str(len(self.sent)))
        reward['Drop'] = {'Items': [{'Equipment': gear}]}
        return reward

    def run_activity(self, **kwargs):
        return ap.run(self.client, 'BH610', 'T', cancel_event=self.cancel,
                      on_progress=self.events.append, account_key='sale-offline', **kwargs)

    def test_sells_before_start_after_ten_new_clears_and_at_end_with_real_inventory_rules(self):
        result = self.run_activity(auto_sell=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.sales_at, [0, 10, 13])
        self.assertEqual(len(self.sale_ids), 13)
        self.assertFalse(set(self.sale_ids) & {'e1', 'e2', 'e3', 'e4', 'e5', 'e6'})
        self.assertEqual(result['progress']['equipmentSales']['sold'], 13)
        self.assertEqual(next(row['count'] for row in result['progress']['equipmentSales']['rewardSummary'] if row['id']=='1'), 1300)

    def test_epic85_is_sold_only_when_explicitly_enabled(self):
        result = self.run_activity(auto_sell=True, include_epic85=True)
        self.assertTrue(result['ok'], result)
        self.assertIn('e1', self.sale_ids)
        self.assertFalse(set(self.sale_ids) & {'e2', 'e3', 'e4', 'e5', 'e6'})

    def test_disabled_sales_never_send_a_sale_request(self):
        result = self.run_activity(auto_sell=False, include_epic85=True)
        self.assertTrue(result['ok'])
        self.assertEqual(self.sales_at, [])
        self.assertEqual(result['progress']['equipmentSales']['sold'], 0)

    def test_old_clears_do_not_count_toward_the_ten_new_clear_interval(self):
        self.clear(1, 2, 3, 4, 5)
        result = self.run_activity(auto_sell=True)
        self.assertTrue(result['ok'])
        self.assertEqual(self.sales_at, [5, 13])

    def test_partial_sale_failure_keeps_confirmed_income_and_pauses_without_retrying(self):
        with patch.object(equipment_sale, 'sell', return_value={
                'ok': False, 'sold': 2, 'detail': '第二批未确认，自动贩卖已暂停',
                'rewardSummary': [{'kind':'Item', 'id':'1', 'name':'金币', 'count':200}]}) as sell:
            result = self.run_activity(auto_sell=True)
        self.assertTrue(result['ok'])
        sell.assert_called_once()
        sales = result['progress']['equipmentSales']
        self.assertFalse(sales['enabled'])
        self.assertEqual(sales['sold'], 2)
        self.assertEqual(sales['rewardSummary'][0]['count'], 200)
        self.assertIn('未确认', sales['error'])

    def test_stop_at_tenth_clear_has_one_final_sale_and_no_duplicate_interval_sale(self):
        def partial(entry):
            if entry['id'] == 'activity_progress' and len(self.sent) == 10:
                self.cancel.set()
        result = self.run_activity(auto_sell=True, on_partial=partial)
        self.assertFalse(result['ok'])
        self.assertEqual(self.sales_at, [0, 10])
        self.assertEqual(len(result['completedScenes']), 10)

    def test_pending_loan_journal_defers_sales_until_return(self):
        pending = {'value': True}
        def elite(client, info, **kwargs):
            pending['value'] = False
            return self.battle(client, info, **kwargs)
        self.elite.side_effect = elite
        with patch.object(abyss_loadout, 'pending', side_effect=lambda *a, **k: pending['value']):
            result = self.run_activity(auto_sell=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.sales_at, [13])


if __name__ == '__main__':
    unittest.main()
