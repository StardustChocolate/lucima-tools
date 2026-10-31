"""Query-index contracts for consumers of the finished encyclopedia cards."""
import unittest

from build_encyclopedia_cards import build_search, normalize, phonetic


def record(hero, name, aliases=(), english="", e7=""):
    return {"id": hero, "name": name, "nameEn": english, "nameE7": e7, "aliases": list(aliases)}


class EncyclopediaCardsTests(unittest.TestCase):
    def test_official_names_and_ambiguous_aliases_keep_separate_candidates(self):
        search = build_search([record("H148", "新春的蜜娜", ["蜜娜"]), record("H804", "蜜娜")])
        self.assertEqual(search["names"]["蜜娜"], ["H804"])
        self.assertEqual(search["aliases"]["蜜娜"], ["H148"])
        self.assertEqual(search["pinyin"]["mina"], ["H148", "H804"])

    def test_unicode_case_whitespace_and_e7_lookup(self):
        search = build_search([record("H001", "夏妮", ["火龙"], english="Shani", e7="塞西莉亚")])
        self.assertEqual(search["ids"][normalize(" Ｈ００１ ")], ["H001"])
        self.assertEqual(search["names"][normalize(" SHANI ")], ["H001"])
        self.assertEqual(search["aliases"]["塞西莉亚"], ["H001"])
        self.assertEqual(search["pinyin"]["huolong"], ["H001"])
        self.assertEqual(normalize("  Little  Devil\tLottie "), "little devil lottie")

    def test_current_source_aliases_and_homophones_preserve_all_candidates(self):
        search = build_search([record("H001", "夏妮", ["火龙"]),
                               record("H002", "菲莉斯", ["火隆"]),
                               record("H607", "玖可", english="Joki")])
        self.assertEqual(search["names"]["joki"], ["H607"])
        self.assertEqual(search["aliases"]["火龙"], ["H001"])
        self.assertEqual(search["pinyin"]["huolong"], ["H001", "H002"])
        self.assertEqual(phonetic("火隆"), "huolong")


if __name__ == "__main__":
    unittest.main()
