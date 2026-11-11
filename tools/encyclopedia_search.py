"""Shared search-index generation for the encyclopedia and finished cards."""
from __future__ import annotations

import unicodedata
from collections import defaultdict


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", str(text)).split()).casefold()


def phonetic(text):
    from pypinyin import Style, lazy_pinyin
    return normalize("".join(lazy_pinyin(char, style=Style.NORMAL)[0]
                             if "\u4e00" <= char <= "\u9fff" else char for char in normalize(text)))


def build_search(records):
    """Keep every candidate; never overwrite a name, alias, or pinyin collision."""
    ids, names, aliases, pinyin = (defaultdict(set) for _ in range(4))
    for row in records:
        hero = row["id"]
        ids[normalize(hero)].add(hero)
        for name in (row.get("name"), row.get("nameEn")):
            if name:
                names[normalize(name)].add(hero)
        for name in (row.get("nameE7"), *(row.get("aliases") or [])):
            if name:
                aliases[normalize(name)].add(hero)
    for table in (ids, names, aliases):
        for word, heroes in table.items():
            pinyin[phonetic(word)].update(heroes)
    return {key: {word: sorted(heroes) for word, heroes in sorted(table.items())}
            for key, table in (("ids", ids), ("names", names), ("aliases", aliases), ("pinyin", pinyin))}
