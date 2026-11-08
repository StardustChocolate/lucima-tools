"""User-defined counter boundaries; HP/elapsed time cannot create a victory."""
import unittest
from backend.abyss_result import resolve_stars, stars_from_counter


class StarFallback(unittest.TestCase):
    def test_all_boundary_values(self):
        for counter, expected in ((0,4),(10,4),(11,3),(12,3),(13,2),(15,2),(16,1),(20,1),(21,0),(100,0)):
            with self.subTest(counter=counter):
                self.assertEqual(stars_from_counter(counter,won=True),expected)

    def test_failed_challenge_cannot_get_stars_from_a_small_counter(self):
        self.assertEqual(stars_from_counter(2,won=False),0)

    def test_unknown_counter_does_not_default_to_four_stars(self):
        for counter in (None,True,False,-1,2.5,'10'):
            with self.subTest(counter=counter):
                self.assertIsNone(stars_from_counter(counter,won=True))

    def test_complete_server_star_flags_have_priority(self):
        self.assertEqual(resolve_stars([1,1,0,0],3,won=True),(2,'server'))

    def test_missing_star_flags_use_the_confirmed_counter(self):
        self.assertEqual(resolve_stars(None,10,won=True),(4,'counter'))
        self.assertEqual(resolve_stars([],12,won=True),(3,'counter'))
        self.assertEqual(resolve_stars([0,0,0],21,won=True),(0,'counter'))

    def test_missing_flags_and_counter_remain_unknown(self):
        self.assertEqual(resolve_stars(None,None,won=True),(None,'unknown'))

    def test_legacy_three_flags_without_counter_never_claim_four_stars(self):
        self.assertEqual(resolve_stars([1,1,1],None,won=True),(3,'server_partial'))


if __name__=='__main__':unittest.main()
