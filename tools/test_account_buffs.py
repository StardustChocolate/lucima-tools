"""Display only active account buffs; innate VIP level perks are excluded."""
from unittest.mock import patch
import unittest

from backend import account_buffs as buffs

NOW=1793440000000


class AccountBuffs(unittest.TestCase):
    def snapshot(self,rows):
        return buffs.snapshot({'StatusContainer':{'List':rows}},NOW)

    def test_vip_perks_are_filtered_by_both_id_and_official_source(self):
        result=self.snapshot([{'StaticID':'VIPHeroExp10'},{'StaticID':'VIPMoneyBonus10'},
                              {'StaticID':'TEST03'},{'StaticID':'TEST04'},
                              {'StaticID':'custom','StatusSource':'VIP'},
                              {'StaticID':'custom2','Source':'VIP'}])
        self.assertTrue(result['available']);self.assertEqual(result['items'],[])

    def test_guild_monthly_card_and_timed_account_buffs_keep_names_and_expiry(self):
        rows=[{'StaticID':'GuildMoneyBonus','EndTime':{'$date':NOW+3600_000}},
              {'StaticID':'TIME06','EndTime':{'$date':NOW+86400_000}},
              {'StaticID':'TIME07','EndTime':{'$date':NOW+86400_000}},
              {'StaticID':'TIME09','EndTime':{'$date':NOW+86400_000}},
              {'StaticID':'TIME04','EndTime':{'$date':NOW+600_000}}]
        result=self.snapshot(rows)['items']
        self.assertEqual([r['source']for r in result],['佣兵团','月卡','月卡','月卡','限时'])
        self.assertEqual(result[1]['name'],result[2]['name'])
        self.assertTrue(all(r['expiresAtMs']>NOW for r in result))
        self.assertTrue(all(r['name']and r['description']for r in result))

    def test_expired_future_duplicate_and_idless_active_entries(self):
        rows=[{'StaticID':'GuildMoneyBonus','EndTime':{'$date':NOW}},
              {'StaticID':'TIME06','StartTime':{'$date':NOW+1}},
              {'StaticID':'TIME07','EndTime':{'$date':NOW+1000}},
              {'StaticID':'TIME07','EndTime':{'$date':NOW+1000}},
              {'StaticID':'ActivityExp'}]
        items=self.snapshot(rows)['items']
        self.assertEqual([r['staticId']for r in items],['TIME07','ActivityExp'])
        self.assertIsNone(items[1]['expiresAtMs'])

    def test_missing_optional_metadata_still_excludes_vip_and_keeps_account_buffs(self):
        with patch.object(buffs,'definitions',return_value={}):
            items=self.snapshot([{'StaticID':'VIPHeroExp10'},{'StaticID':'TIME06'},{'StaticID':'GuildMoneyBonus'}])['items']
        self.assertEqual([r['staticId']for r in items],['TIME06','GuildMoneyBonus'])


if __name__=='__main__':unittest.main()
